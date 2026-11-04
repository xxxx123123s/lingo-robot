#include "stm32f10x.h"                  // Device header
#include "Delay.h"
#include "OLED.h"
#include "LED.h"
#include "Timer.h"
#include "Key.h"
#include "MPU6050.h"
#include "Motor.h"
#include "Encoder.h"
#include "Serial.h"
#include "BlueSerial.h"
#include "PID.h"
#include "math.h"
#include <string.h>
#include <stdlib.h>

#define Alpha 0.001			//互补滤波系数，时间常数 τ = 0.01s/0.001 = 10s

/*角度零位校准值：让平衡姿态时的输出 Angle = 0。
  当前写法是 Angle = AngleRaw - ANGLE_OFFSET，所以：
    把车扶到能自平衡的那个姿态，看 OLED 左下角的 aA:（加速度计角），
    ANGLE_OFFSET 就写成该读数本身。
    例：aA 显示 9.0 → 写 9.0f；显示 6.5 → 写 6.5f

  ★本轮标定：加阻尼前实测振荡在 0 ~ -7° 之间，中心在 -3.5°（不是 0），
    说明平衡点整体偏低 3.5°。因为 Angle = AngleRaw - OFFSET，
    要让中心抬高 3.5°，就要把 OFFSET 减掉 3.5：5.0 - 3.5 = 1.5。
    改完振荡应变成 -3.5 ~ +3.5，关于 0 对称*/
#define ANGLE_OFFSET 1.5f

/*编码器计数方向。正常情况都是 +1.0f，只有实测发现符号反了才改（判定方法见中断里速度换算处）。
  分成左右两个，是为了能单独处理"两个轮子符号相反、aveSpeed 相互抵消"的情况*/
#define SPEED_SIGN_L  (+1.0f)		//左轮（TIM3，PA6/PA7）
#define SPEED_SIGN_R  (+1.0f)		//右轮（TIM4，PB6/PB7）

/*蓝牙波形的"速度显示增益"：只影响画出来的线高矮，完全不参与控制。
  为什么需要它：aveSpeed 的量级是 0~2（圈/秒），Angle 的量级是 ±7（度），
  同一条纵轴上画，速度线会贴在底部成一条直线、看不出变化。
  想让它和角度幅度大致相当就把这个值调大（速度 1.5 圈/秒 ≈ 角度 5° → 用 3.0 左右）。
  读数时记得除以它才是真实速度：显示 6.0 就是 2.0 圈/秒（当 GAIN=3 时）*/
#define PLOT_SPD_GAIN  3.0f

float AngleA;//角度
float AngleG;//陀螺仪测角度角度
float AngleRaw;//互补滤波原始角度（未加零位补偿）
float Angle;//校准后的角度，平衡位置为0°

/*================= 零点自动校准 =================
  最终零位 = ANGLE_OFFSET(手工初值) + AngleOfsLearn(自动学习量)

  原理：车"走稳"的时候（静止站住，或者匀速巡航），车身没有加速度，
        所以物理倾角＝0，角度环稳态 Angle=Target，而 Angle = 平衡点 - 零位 = -δ，
        于是 AnglePID.Target 恒等于 -δ —— 它就是"零点偏了多少"（带负号）。
        把这个 1.5 秒平均值加到零位里，Angle 就自动收敛到 0，
        不用每次烧录、读 aA、试一个数再烧一遍。

  为什么不能用加速度计角 aA 直接当零点：
        aA 是"相对重力竖直方向"的倾角，而车的平衡点由重心位置、
        电池位置、传感器安装角共同决定，两者通常差 1~5°。
        拿 aA 当 0，车会一直带着一个前倾/后仰量往前冲。

  安全设计（任何一条不满足就丢弃整个窗口，不学）：
    ① 必须在跑（RunFlag=1）、没倒地（|Angle|<15）、输出没顶饱和（|Out|<85）
    ② 窗口内一次转向都不能有（TurnPID.Target 必须一直为 0）
    ③ 窗口内速度必须稳定（峰峰 <0.10 圈/秒）且不快于 3 圈/秒
       ★注意这里是"稳定"不是"为零"：匀速巡航时同样能学。
         如果要求"必须停下"，偏差大时车根本停不下来，就永远学不到 —— 死循环
    ④ 窗口内角度峰峰值 <15°（剧烈晃动/被抱起来不算）
    ⑤ 窗口内倾角指令必须平直（峰峰 <0.5°）：速度环没稳时的平均值是偏的，不能拿来学
    ⑥ 偏差本身太小（|T|<0.15°）就不动手，避免噪声把零位磨来磨去
    ⑦ 学习量总限幅 ±15°；每次只走 70%（步长），错了下一轮能自己纠正回来
    ⑧ 每次学完冷却 2.5 秒，等速度环把新零位消化掉，避免"学过头再学回来"

  已知边界：
    · 学习量只存在 RAM 里，断电就没了。每次上电会从 ANGLE_OFFSET 重新学；
      觉得学得好的话，把 Z: 的值加到 ANGLE_OFFSET 里烧回去，就不必每次重学。
  ================================================*/
#define ANGLE_AUTOCAL      1        //1=开启自动校准，0=关闭（只用 ANGLE_OFFSET 宏）
#define AUTOCAL_STEP       0.7f     //每次应用多少比例：1.0=一次到位，0.7=两三次收敛
#define AUTOCAL_LEARN_MAX  15.0f    //学习总量限幅 ±15°
#define AUTOCAL_NEED       15       //需要连续满足 15×100ms = 1.5 秒
#define AUTOCAL_V_PP       0.10f    //窗口内速度峰峰上限：比这还大说明正在加速/减速
#define AUTOCAL_VABS       3.0f     //速度绝对值上限（圈/秒）：太快时编码器噪声大，不学
#define AUTOCAL_SPAN_MAX   15.0f    //窗口内角度峰峰上限（°）
#define AUTOCAL_TMIN       0.15f    //偏差小于这个就不学（°）
#define AUTOCAL_T_PP       0.50f    //窗口内倾角指令的峰峰上限（°）：比这还抖说明速度环没稳
#define AUTOCAL_COOL_MS    2500u    //学完冷却 2.5 秒，等速度环把新零位消化掉

float AngleOfsLearn = 0;    //自动学习到的零位修正量，OLED 上显示为 Z:

/*显示用平滑角：只给 OLED 和蓝牙波形看，控制环仍然用原始的 Angle。
  为什么要它：屏幕上和波形上这个数是 100Hz 刷新的，肉眼看到的是"每秒跳 100 次"，
  读不出趋势，也没法用来标 ANGLE_OFFSET。
  一阶低通：Alpha=0.05 → 时间常数 τ = 0.01/0.05 = 0.2 秒。
  想要更稳就调小（0.02 → τ=0.5s，但跟手变差）；想更跟手就调大（0.1）。*/
#define ANGLE_DISP_ALPHA 0.05f
float AngleDisp = 0;

/*==================== 松手诊断快照 ====================
  现象是"松手后车自己跑"，但屏幕上全是实时跳动的数，人根本读不下来。
  所以松手瞬间自动抓一组，1 秒后再抓一组，然后冻结显示，
  你只管把车捡回来、抬头看屏幕。
    SnapState = 0 正常调参页（松手后自动进入 1）
    SnapState = 1 已抓 t=0，等 t=1s
    SnapState = 2 两组都抓到了，冻结，重新推摇杆才清掉
  T0 / T1 依次是：Angle、AnglePID.Target、SpeedPID.Out、
                速度环积分贡献、aveSpeed、difSpeed、AngleA
=======================================================*/
volatile uint32_t MsTick;			/*中断里每 1ms 加一，给主循环计时用*/
float Snap0[7], Snap1[7];
uint8_t SnapState = 0;
uint32_t SnapT0;
int8_t LastRH = 0;
int8_t LastLV = 0;
uint32_t LastJoyTick = 0;			/*最后一次收到摇杆包的时刻，用来做失联保护*/
volatile uint8_t PlotFlag = 0;		/*1 = 20Hz 采样到了新数据，待主循环发波形*/


int16_t AX, AY, AZ, GX, GY, GZ;
uint8_t TimerErrorFlag;
uint16_t Timercount0;

uint8_t KeyNum,RunFlag;

int16_t LeftPWM, RightPWM;
int16_t avePWM,difPWM;

float  LeftSpeed, RightSpeed;
float  aveSpeed,difSpeed;
PID_t SpeedPID={

 .Target = 0,
 .OutMax=8,			//速度环输出的是"倾角指令"，±8°已经足够加速；原来是 ±20°，
 .OutMin=-8,		//等于允许它命令车身前倾 20°，一旦给满车必窜出去
 .Kp = 2.5,
 .Ki = 0.01,		//★原来是 0.01，20Hz 下积分几乎不干活：留 3° 前倾要 10 秒才放得掉，
					//表现出来就是"松手后车照原速继续跑"。加大到 0.15 后 0.3~0.5 秒就能反过来刹停
 .Kd = 0,
 .IntMax = 4,		//积分最多贡献 4° 倾角指令
					//★这个上限决定了"零点误差"能被补掉多少：车静止时速度环要输出 -δ 才能抵消
					//  ANGLE_OFFSET 的 δ 度偏差，积分上限必须大于 δ，否则车会一直以 v=δ/Kp 巡航
};

PID_t AnglePID={

 .Target = 0,
 .OutMax=100,
 .OutMin=-100,
 .Kp = 2.81,
 .Ki = 0.26,
 /*★阻尼单位变了：开启 UseDIn 后，Kd 的单位是"每 1°/s 给几个 PWM"，不再是"每拍变化 1° 给几个 PWM"。
  原来的 Kd=4 等价于 4×0.01 = 0.04 PWM/(°/s)，等于没有阻尼 —— 这就是车以 3~5Hz 来回荡、
  角度在 0 到 -7° 之间周期摆动的原因（D 项根本没在刹车）。
  0.6 是按"Kd=60 的差分版实测阻尼够用"换算过来的：60×0.01 = 0.6。
  ★调法：还荡就往上加（0.8 → 1.0）；出现高频"嗡嗡"抖或电机发烫就往下减（0.4 → 0.3）*/
 .Kd = 0.11,
 .DIn = 0,
 .UseDIn = 1,		//角度环的 D 项直接用陀螺仪角速度（在中断里每拍更新）
 .IntMax = 12,		//★这里是猛冲的根源：以前积分最多能攒到 50 个 PWM（半个量程），
};					//前进行驶时外环给的前倾指令会被它当成误差攒起来，松手时一次性放出来。
					//现在压到 12 个 PWM，只够消机械静差

PID_t TurnPID={

 .Target = 0,
 .OutMax=20,		//差速限幅：现在改成"全量叠加"，所以这个值就是每轮的实际差速 PWM。
 .OutMin=-20,		//原来 25 配 /2 等于每轮 ±12.5，改成全量后必须一起降，否则转速直接翻倍。
					//±20 已远高于电机静摩擦死区（8~10），不会再有"一侧不转"的情况
 .Kp = 5,
 .Ki = 4,
 .Kd = 0,
 .IntMax = 8,		//积分最多贡献 8 个差速 PWM
};
void Snap_Take(float *d)
{
	d[0] = Angle;								/*融合角*/
	d[1] = AnglePID.Target;						/*角度环目标＝倾角指令*/
	d[2] = SpeedPID.Out;						/*速度环输出*/
	d[3] = SpeedPID.Ki * SpeedPID.ErrorInt;		/*★速度环积分贡献：松手后它必须变号，才能把车刹停*/
	d[4] = aveSpeed;							/*★实测速度：松手后必须真的往 0 掉*/
	d[5] = difSpeed;							/*差速*/
	d[6] = AngleA;								/*加速度计角，看姿态有没有漂*/
}

/*================= 零点自动校准（主循环调用，每 100ms 判一次）=================
  用"速度环稳态目标 T:"当依据，而不是角度本身：T 是速度环想要的车身倾角，
  车真站住没走时，它就正好等于"零点偏差的反号"，且完全不受车身振荡影响
  （角在 ±3.5 来回摆的时候，T 是一条平直线）。

  ★为什么必须要求 T 平直（峰峰 < 0.5°）：
    学完一次后零位变了，但速度环的积分还"记着"旧值，要 3 秒左右才重新稳定。
    这段过渡期里 T 的平均值是偏的，拿它学习就会"学过头，再学回来"地来回震荡。
    加上这条判据后，过渡期整段被跳过，实测 13 秒收敛、不过冲。
  ============================================================================*/
void Angle_AutoCal(void)
{
#if ANGLE_AUTOCAL
	static float    SumT, SumV;
	static float    AngMin, AngMax;
	static float    TMin, TMax;
	static float    VMin, VMax;
	static uint16_t N;
	static uint32_t LastT, CoolT;

	uint32_t now = MsTick;
	float tAvg, vAvg;

	/*100ms 采样一次，别让主循环的快慢影响窗口长度*/
	if ((uint32_t)(now - LastT) < 100) return;
	LastT = now;

	/*刚学过：冷却一段时间，等速度环把新的零点消化掉再判，否则会自己跟自己打架*/
	if ((uint32_t)(now - CoolT) < AUTOCAL_COOL_MS) { N = 0; return; }

	/*① 前置条件：在跑、没倒地、输出没顶饱和（顶饱和说明车被卡住/顶墙了）*/
	if (RunFlag == 0 ||
	    Angle > 15.0f || Angle < -15.0f ||
	    AnglePID.Out > 85.0f || AnglePID.Out < -85.0f)
	{
		N = 0;
		return;
	}

	/*② 窗口里只要出现过转向指令，整段作废（原地转向时 v 和 T 都没意义）*/
	if (TurnPID.Target != 0.0f) { N = 0; return; }

	/*开新窗口：清掉累加器，用当前值初始化峰峰值*/
	if (N == 0)
	{
		SumT = 0; SumV = 0;
		AngMin = Angle; AngMax = Angle;
		TMin = AnglePID.Target; TMax = AnglePID.Target;
		VMin = aveSpeed; VMax = aveSpeed;
	}

	if (Angle < AngMin) AngMin = Angle;
	if (Angle > AngMax) AngMax = Angle;
	if (AnglePID.Target < TMin) TMin = AnglePID.Target;
	if (AnglePID.Target > TMax) TMax = AnglePID.Target;
	if (aveSpeed < VMin) VMin = aveSpeed;
	if (aveSpeed > VMax) VMax = aveSpeed;
	SumT += AnglePID.Target;
	SumV += aveSpeed;
	N ++;

	if (N < AUTOCAL_NEED) return;			/*还没攒够 1.5 秒*/

	/*窗口到期，开始判*/
	tAvg = SumT / (float)N;
	vAvg = SumV / (float)N;
	N = 0;									/*不管学不学，窗口都重新开始*/

	/*③ 车在变速（加速/减速过程中）→ 这时的 T 是"想加速"而不是"补零点"，不能学。

	  ★这一条原来是"平均速度必须≈0"（只能停下才学），但那会造成死循环：
	    零点偏差大时，纯 P 速度环的稳态速度就是 δ/Kp（3.5° 就对应 1.75 圈/秒），
	    车永远"匀速往前跑、停不下来"；停不下来就永远不满足"v≈0"，也就永远学不到，
	    偏差永远存在。—— 这就是"松手停不下来"的闭环。

	  改判"速度稳定"就够了：匀速行驶时车身倾角≈0（没有加速度），
	  角度环稳态 Angle=Target，而 Angle = 平衡点 - 零位 = -δ，
	  所以此时 T 恒等于 -δ，正是要学的那个量。车上车下都成立。*/
	if (VMax - VMin > AUTOCAL_V_PP) return;			/*速度还在变，说明在加减速*/
	if (vAvg > AUTOCAL_VABS || vAvg < -AUTOCAL_VABS) return;	/*太快，噪声大，不学*/

	/*③.5 倾角指令已经顶到限幅 → 此刻它代表"拼命加速"，不是"零点偏了多少"。
	  零点偏差超过 OutMax(8°) 时就是这种状态，学它会越学越偏（正反馈）。
	  这种情况只能手改 ANGLE_OFFSET，不能自动学。*/
	if (tAvg >  0.90f * SpeedPID.OutMax) return;
	if (tAvg < -0.90f * SpeedPID.OutMax) return;

	/*④ 窗口内晃动过大 / 被抱起来过 → 数据不干净*/
	if (AngMax - AngMin > AUTOCAL_SPAN_MAX) return;

	/*⑤ 倾角指令还在变（速度环没稳）→ 平均值不能用，跳过这一窗，等它稳*/
	if (TMax - TMin > AUTOCAL_T_PP) return;

	/*⑥ 偏差本来就很小 → 什么都不做，避免噪声把零位一点点磨偏*/
	if (tAvg < AUTOCAL_TMIN && tAvg > -AUTOCAL_TMIN) return;

	/*⑦ 学习：T>0 表示"车实际平衡点比当前零位高"，零位就要跟着加。
	      （Angle = AngleRaw - 零位，零位加大 → Angle 变小 → 平衡点被搬到 0）
	      只走 STEP 比例，多来几次自然收敛，单次学错也不会一下子偏出去*/
	AngleOfsLearn += AUTOCAL_STEP * tAvg;
	if (AngleOfsLearn >  AUTOCAL_LEARN_MAX) AngleOfsLearn =  AUTOCAL_LEARN_MAX;
	if (AngleOfsLearn < -AUTOCAL_LEARN_MAX) AngleOfsLearn = -AUTOCAL_LEARN_MAX;

	CoolT = now;							/*进入冷却*/
#endif
}

int main(void)
{
 /*模块初始化*/
 OLED_Init();		
 MPU6050_Init();		

 Serial_Init();
 Motor_Init();
 Encoder_Init();
 BlueSerial_Init();
 LED_Init();
 Key_Init();
 
 Timer_Init();		
 while (1)
 {
  KeyNum=Key_GetNum();
  if(KeyNum==1)
  {
    RunFlag=!RunFlag;
	PID_init(&AnglePID);
	PID_init(&SpeedPID);
	PID_init(&TurnPID);
  }

  if(RunFlag)
  {
    LED_ON();
  }
  else
  {
    LED_OFF();
  }





  /*松手满 1 秒，抓第二组诊断数据，之后画面冻结（重新推摇杆才清掉）*/
  if (SnapState == 1 && (uint32_t)(MsTick - SnapT0) >= 1000)
  {
	Snap_Take(Snap1);
	SnapState = 2;
  }

  /*★摇杆失联保护：超过 500ms 没收到摇杆包（遥控器关了 / 超范围 / 蓝牙掉线），
    就把行进和转向指令全部归零。
    没有这一条时，Target 会一直卡在最后一次收到的值上：遥控器已经不发了，
    车却还以为你要它以 5 圈/秒往前跑，于是怎么松手都停不下来 ——
    这也是"松摇杆却停不住"的一个常见原因，和积分无关。*/
  if (RunFlag == 1 && (uint32_t)(MsTick - LastJoyTick) > 500)
  {
	if (SpeedPID.Target != 0.0f || TurnPID.Target != 0.0f)
	{
		SpeedPID.Target = 0.0f;
		TurnPID.Target  = 0.0f;
		SpeedPID.ErrorInt = 0;			/*同"松手"处理：不留历史前倾指令*/
		TurnPID.ErrorInt  = 0;
		AnglePID.ErrorInt = 0;
		LastRH = 0;
		LastLV = 0;
	}
  }

  /*零点自动校准：满足"站住了 2 秒"就用速度环的倾角指令把零位慢慢归到 0*/
  Angle_AutoCal();

  /*OLED显示*/
 OLED_Clear();
 if (SnapState == 0)				/*正常：PID 调参页*/
 {
		OLED_Printf(0, 0, OLED_6X8, "  Angle");
		OLED_Printf(0, 8, OLED_6X8, "P:%05.2f", AnglePID.Kp);
		OLED_Printf(0, 16, OLED_6X8, "I:%05.2f", AnglePID.Ki);
		OLED_Printf(0, 24, OLED_6X8, "D:%05.2f", AnglePID.Kd);
		OLED_Printf(0, 32, OLED_6X8, "T:%+05.1f", AnglePID.Target);
		OLED_Printf(0, 40, OLED_6X8, "A:%+05.1f", AngleDisp);	//显示用平滑值，读数才稳；控制仍用 Angle
		OLED_Printf(0, 48, OLED_6X8, "O:%+05.0f", AnglePID.Out);
		OLED_Printf(0, 56, OLED_6X8, "aA:%+05.1f", AngleA);
		OLED_Printf(50, 0, OLED_6X8, "Speed");
		OLED_Printf(50, 8, OLED_6X8, "%05.2f", SpeedPID.Kp);
		OLED_Printf(50, 16, OLED_6X8, "%05.2f", SpeedPID.Ki);
		OLED_Printf(50, 24, OLED_6X8, "%05.2f", SpeedPID.Kd);
		OLED_Printf(50, 32, OLED_6X8, "%+05.1f", SpeedPID.Target);
		OLED_Printf(50, 40, OLED_6X8, "%+05.1f", aveSpeed);
		OLED_Printf(50, 48, OLED_6X8, "%+05.0f", SpeedPID.Out);
		OLED_Printf(50, 56, OLED_6X8, "Z:%+05.1f", AngleOfsLearn);	//零点自动校准学习量：归零到 0 就是标好了
		OLED_Printf(88, 0, OLED_6X8, "Turn");
		OLED_Printf(88, 8, OLED_6X8, "%05.2f", TurnPID.Kp);
		OLED_Printf(88, 16, OLED_6X8, "%05.2f", TurnPID.Ki);
		OLED_Printf(88, 24, OLED_6X8, "%05.2f", TurnPID.Kd);
		OLED_Printf(88, 32, OLED_6X8, "%+05.1f", TurnPID.Target);
		OLED_Printf(88, 40, OLED_6X8, "%+05.1f", difSpeed);
		OLED_Printf(88, 48, OLED_6X8, "%+05.0f", TurnPID.Out);
 }
 else								/*松手诊断页：左列=松手瞬间，右列=1 秒后*/
 {
		OLED_Printf(0,  0, OLED_6X8, "DIAG");
		OLED_Printf(54, 0, OLED_6X8, "now");
		OLED_Printf(96, 0, OLED_6X8, "+1s");

		OLED_Printf(0,  8, OLED_6X8, "A  %+05.1f", Snap0[0]);	/*融合角*/
		if (SnapState == 2) OLED_Printf(96, 8, OLED_6X8, "%+05.1f", Snap1[0]);
		OLED_Printf(0, 16, OLED_6X8, "tg %+05.1f", Snap0[1]);	/*角度环目标=倾角指令*/
		if (SnapState == 2) OLED_Printf(96, 16, OLED_6X8, "%+05.1f", Snap1[1]);
		OLED_Printf(0, 24, OLED_6X8, "sp %+05.1f", Snap0[2]);	/*速度环输出*/
		if (SnapState == 2) OLED_Printf(96, 24, OLED_6X8, "%+05.1f", Snap1[2]);
		OLED_Printf(0, 32, OLED_6X8, "si %+05.1f", Snap0[3]);	/*★速度环积分贡献*/
		if (SnapState == 2) OLED_Printf(96, 32, OLED_6X8, "%+05.1f", Snap1[3]);
		OLED_Printf(0, 40, OLED_6X8, "v  %+05.1f", Snap0[4]);	/*★实测速度*/
		if (SnapState == 2) OLED_Printf(96, 40, OLED_6X8, "%+05.1f", Snap1[4]);
		OLED_Printf(0, 48, OLED_6X8, "dv %+05.1f", Snap0[5]);	/*差速*/
		if (SnapState == 2) OLED_Printf(96, 48, OLED_6X8, "%+05.1f", Snap1[5]);
		OLED_Printf(0, 56, OLED_6X8, "aA %+05.1f", Snap0[6]);	/*加速度计角*/
		if (SnapState == 2) OLED_Printf(96, 56, OLED_6X8, "%+05.1f", Snap1[6]);
 }
		OLED_Update();



	
	
	/*蓝牙串口接收数据包处理*/
		/*规定的数据包格式为：[数据1,数据2,数据3,...]*/
		if (BlueSerial_RxFlag == 1)		//如果收到数据包
		{
			char *Tag = strtok(BlueSerial_RxPacket, ",");	//提取数据1，定义为标签Tag
			if (strcmp(Tag, "key") == 0)					//Tag为key，收到按键数据包
			{
				char *Name = strtok(NULL, ",");				//提取数据2，定义为按键名称
				char *Action = strtok(NULL, ",");			//提取数据3，定义为按键动作
				
				/*此处可执行按键操作，目前程序暂时没用到按键*/
			}
			else if (strcmp(Tag, "slider") == 0)			//Tag为slider，收到滑杆数据包
			{
				char *Name = strtok(NULL, ",");				//提取数据2，定义为滑杆名称
				char *Value = strtok(NULL, ",");			//提取数据3，定义为滑杆值
				
				/*执行滑杆操作*/
				if (strcmp(Name, "AngleKp") == 0)			//如果滑杆名称是AngleKp
				{
					AnglePID.Kp = atof(Value);				//则把滑杆值赋值给角度环Kp
				}
				else if (strcmp(Name, "AngleKi") == 0)		//如果滑杆名称是AngleKi
				{
					AnglePID.Ki = atof(Value);				//则把滑杆值赋值给角度环Ki
				}
				else if (strcmp(Name, "AngleKd") == 0)		//如果滑杆名称是AngleKd
				{
					AnglePID.Kd = atof(Value);				//则把滑杆值赋值给角度环Kd
				}
				else if (strcmp(Name, "SpeedKp") == 0)
				{
					SpeedPID.Kp = atof(Value);
				}
				else if (strcmp(Name, "SpeedKi") == 0)
				{
					SpeedPID.Ki = atof(Value);
				}
				else if (strcmp(Name, "SpeedKd") == 0)
				{
					SpeedPID.Kd = atof(Value);
				}
				else if (strcmp(Name, "TurnKp") == 0)
				{
					TurnPID.Kp = atof(Value);
				}
				else if (strcmp(Name, "TurnKi") == 0)
				{
					TurnPID.Ki = atof(Value);
				}
				else if (strcmp(Name, "TurnKd") == 0)
				{
					TurnPID.Kd = atof(Value);
				}
			}
			else if (strcmp(Tag, "joystick") == 0)			//Tag为joystick，收到摇杆数据包
			{
				int8_t LH = atoi(strtok(NULL, ","));		//提取数据2，定义为摇杆值LH
				int8_t LV = atoi(strtok(NULL, ","));		//提取数据3，定义为摇杆值LV
				int8_t RH = atoi(strtok(NULL, ","));		//提取数据4，定义为摇杆值RH
				int8_t RV = atoi(strtok(NULL, ","));		//提取数据5，定义为摇杆值RV
				uint8_t RelFlag;							//1=这一拍刚好松开摇杆
				
				LastJoyTick = MsTick;						//刷新"最后一次收到摇杆包"的时刻

				/*执行摇杆操作*/
				SpeedPID.Target = LV / 25.0;		//摇杆值LV缩2放后，控制角度环目标值，前后行进控制
				TurnPID.Target = RH / 5.0;				//摇杆值RH缩放后，控制差分PWM，左右转弯控制
				
				/*判断"刚刚松开摇杆"：任意一个方向从非零回到零的那一拍。
				  ★这里必须用"边沿"判断，不能写成 if(RH==0)。
				  原来的写法只要 RH 是 0 就一直成立，而直行时 RH 本来就是 0 ——
				  于是每收到一包蓝牙数据就把速度积分和角度积分清一次，
				  速度环等于永远没有积分，只剩一个很弱的 P，
				  松手后连"反着倾一下把车刹住"的力气都没有，车就照原速往前跑。*/
				RelFlag = 0;
				if ((RH == 0 && LastRH != 0) || (LV == 0 && LastLV != 0))
				{
					RelFlag = 1;
				}

				if (RelFlag)
				{
					Snap_Take(Snap0);			//刚刚松开的那一瞬：抓第一组诊断数据
					SnapT0 = MsTick;
					SnapState = 1;

					TurnPID.ErrorInt  = 0;		//转向积分：松手即失效，否则差速残留
					AnglePID.ErrorInt = 0;		//角度环积分：清掉，防"猛冲"
					/*★速度环积分也清掉（原来是"故意保留"，那是错的）。
					  推前进杆时它一直在往正方向累，最多积到 IntMax=4° 的前倾指令。
					  松手瞬间 Target 变 0，但 Out = Kp*(0-v) + 积分 = -2v + 4：
					  只要车速低于 2 圈/秒，这个值仍然是【正的】——也就是车还在被
					  要求"继续前倾"，于是越跑越快，看起来就是"松手停不下来、
					  误差积分太大"。
					  清掉之后 Out 立刻变成 -2v（后仰），车当场停止加速并开始减速；
					  刹停过程中速度环会重新把积分积成正确的 -δ 补偿值，不影响稳态。*/
					SpeedPID.ErrorInt = 0;
				}
				if (RH != 0 || LV != 0)			//重新推杆，回到调参页
				{
					SnapState = 0;
				}
				LastRH = RH;
				LastLV = LV;
			}
			
			BlueSerial_RxFlag = 0;				//处理完成后，标志位置0，允许接收下一个数据包
		}
		
		/*蓝牙串口打印波形，需配合蓝牙串口小程序实现波形绘制
		  ★当前接的是"平均速度 + 实际角度"这一组：
		    第一条 = aveSpeed×PLOT_SPD_GAIN  两轮平均速度（真实值要再除以 PLOT_SPD_GAIN）
		    第二条 = Angle                    车体实际倾角（就是参与 PID 的那个值，平衡时应≈0）
		  看点：
		    ─ 车匀速行进时，两条线应该各自平稳：速度保持在某个值、角度≈0。
		      如果"速度一直涨、角度也一直偏" → 倾角指令/零点还在推车（看 T: 和 Z:）
		    ─ 松摇杆后：速度线应迅速回落到 0，角度线应回到 0 附近。
		      速度线不回落但角度≈0 → 不是姿态问题，回到速度环/编码器那一路
		    ─ 角度线是一团毛刺 → 高频抖动，必要时开 MPU6050 的 DLPF（见 MPU6050.c）
		  只在 20Hz 采样更新后发一次，保证波形横轴是等间隔的（9600 波特率下
		  一行要阻塞 ~20ms，若每圈主循环都发，时间轴会被主循环速度带偏）。
		  一行 ~24 字符 ≈ 25ms，20Hz（50ms）够用；若改到 100Hz 发送必须提波特率*/
		if (PlotFlag)
		{
			PlotFlag = 0;
			BlueSerial_Printf("[plot,%.3f,%.3f]", aveSpeed * PLOT_SPD_GAIN, Angle);
		}
		// BlueSerial_Printf("[plot,%f,%f]", TurnPID.Target, difSpeed);
		// BlueSerial_Printf("[plot,%f,%f]", SpeedPID.Target, aveSpeed);
		// BlueSerial_Printf("[plot,%.2f,%.2f]", Angle, AngleDisp);
		// BlueSerial_Printf("[plot,%.3f,%.3f]", LeftSpeed, RightSpeed);
 }
}
void TIM1_UP_IRQHandler(void)
{
  static uint8_t count0,count1;
 if (TIM_GetITStatus(TIM1, TIM_IT_Update) == SET)
 {
  
  TIM_ClearITPendingBit(TIM1, TIM_IT_Update);
  Key_Tick();
  
  MsTick++;						/*1ms 时基，主循环用它算"松手后过了多久"*/





  
  count0++;
  if(count0>=10)//测量周期正大互补滤波系数也要增大
  {
		count0=0;
		MPU6050_GetData(&AX, &AY, &AZ, &GX, &GY, &GZ);

		AngleA=-atan2(AX,AZ)/3.1415926*180;
		
		/*互补滤波在"传感器原始坐标系"里做，避免把零位补偿量累加进陀螺仪积分*/
		AngleG=AngleRaw+GY/32768.0f*2000.0f*0.01f;
		
		/*固定系数的互补滤波：加速度计每拍只纠正 Alpha=0.1%，时间常数 10 秒*/
		AngleRaw=Alpha*AngleA+(1-Alpha)*AngleG;
		
		/*加零位补偿后再输出：手工初值 + 自动学习量，平衡时 Angle=0*/
		Angle=AngleRaw-(ANGLE_OFFSET+AngleOfsLearn);

		/*显示用平滑（不影响控制）：第一拍直接取实测值，避免从 0 慢慢爬上来*/
		{
			static uint8_t First = 1;
			if (First) {AngleDisp = Angle; First = 0;}
			else       {AngleDisp += ANGLE_DISP_ALPHA * (Angle - AngleDisp);}
		}
		if(RunFlag==1)
		{
				if(Angle>50||Angle<-50)
			{
				RunFlag=0;
				Motor_SetPWM(1,0);
				Motor_SetPWM(2,0);
			}
			else
			{
							 
				
				
				AnglePID.Actual=Angle;
				/*D 项用的角速度：陀螺仪直接给，单位 °/s（与 AngleG 积分用的比例一致）。
				  这就是"对角度求导"的真值，比把融合角相邻两拍相减干净得多*/
				AnglePID.DIn=GY/32768.0f*2000.0f;
				PID_Update(&AnglePID);
				avePWM=AnglePID.Out;

				/*★差速全量叠加，不再除以 2。
				  原地转弯时"绕一边跑"的直接原因就在这里：difPWM 上限 25，
				  除以 2 后每轮只有 ±12.5，而平衡环的 avePWM 静态就在 ±5~10 抖动，
				  两者同量级 —— 于是总有一侧的净输出被抵消到电机静摩擦死区（8~10）以内，
				  那个轮子直接不转，另一侧在转，车就变成"右边绕着左边跑"（还带往前漂）。
				  全量叠加后每轮差速 ±25，共模要涨到 25 才会吃掉一侧，余量翻倍。*/
				LeftPWM=avePWM-difPWM;
				RightPWM=avePWM+difPWM;

				if(LeftPWM>100){LeftPWM=100;}
				else if(LeftPWM<-100){LeftPWM=-100;}
				if(RightPWM>100){RightPWM=100;}
				else if(RightPWM<-100){RightPWM=-100;}

				Motor_SetPWM(1,LeftPWM);
				Motor_SetPWM(2,RightPWM);
			}
			 
		}
		else
		{
			Motor_SetPWM(1,0);
			Motor_SetPWM(2,0);
		}
    
  }

 count1++;
 if(count1>=50)
 {
	count1=0;
	/*编码器符号（出厂 +1，改动前先看下面的判定方法，不要乱试）：
	  测法：通电、【不要按启动键】（电机不转），用手把车往前推一下
	        ─ 看 (50,40) 的 aveSpeed：应该是正数，推得越快越大 → 符号正确，别动这里
	        ─ 是负数 → 两个都反了，把 L、R 都改成 -1.0f
	        ─ 接近 0 不动，而 (88,40) 的 difSpeed 很大 → 左右相反，把其中一个改成 -1.0f
	  符号错了的后果很严重：速度环会变成正反馈，一下指令就越跑越快、刹不住*/
	LeftSpeed =SPEED_SIGN_L*Encoder_Get(1)/44.0/0.05/9.27666;
	RightSpeed=SPEED_SIGN_R*Encoder_Get(2)/44.0/0.05/9.27666;
	difSpeed=LeftSpeed-RightSpeed;
	aveSpeed=(LeftSpeed+RightSpeed)/2.0; 
	PlotFlag=1;			/*本拍有新数据，让主循环去发一次波形（20Hz 定速，波形时间轴才不乱）*/
	if(RunFlag==1)
	{
	/*先算转向环：它只约束"两轮速度差"*/
	TurnPID.Actual=difSpeed;
	PID_Update(&TurnPID);
	difPWM=TurnPID.Out;

	/*★速度环【任何时候都要跑】，不再像以前那样"转向时直接退出"。
	  原因：差速环只约束 L-R，两轮之和 L+R 没人管。和不为零就等于车在平移，
	  瞬时转动中心会从车体中间滑到某一侧的轮子上 —— 也就是"右边绕着另一边跑"，
	  而且一边转一边往前漂。速度环是唯一能把这个"和"压回目标值的东西。

	  但转向期间【不许它积分】：原地转向车轮打滑，aveSpeed 会有偏，
	  积起来就成了松手后还挂着的残留倾角（就是之前"转久了松手往前冲"的根源）。
	  所以转向期间只保留 P 的即时作用，积分器每拍清零、不许累积。*/
	if(TurnPID.Target != 0)
	{
		SpeedPID.ErrorInt = 0;		
	}
	SpeedPID.Actual=aveSpeed;
	PID_Update(&SpeedPID);
	AnglePID.Target=SpeedPID.Out;		/*倾角指令：不打前进杆时目标就是 0，
										  车只转不平移；打了前进杆就是边走边转*/

	/*★松手防溜车钳位（纯逻辑，不改任何 PID 参数）
	  条件：没有前进指令 + 车确实在往前跑（v>0.10）+ 倾角指令却是"前倾"（正）。
	  含义：既然没要求往前走，就绝不允许车还保持着前倾姿态。
	  哪怕速度环积分又慢慢积回来一点，这一句也会把前倾指令压成 0，
	  车立刻停止加速，只剩 P 项的 -Kp*v 在把它往后仰、把速度刹掉。
	  方向注意：只在 v>0 时钳。车往后溜时（v<0）仍然允许正倾角去刹，
	  不会被卡死在"越溜越快"，两边都能停。*/
	if (SpeedPID.Target == 0.0f && aveSpeed > 0.10f && AnglePID.Target > 0.0f)
	{
		AnglePID.Target = 0.0f;
	}
	}



 }
 






  /*中断函数退出前，再次检查标志位*/
  if (TIM_GetITStatus(TIM1, TIM_IT_Update) == SET)
  {
   /*标志位又置1了，说明中断函数执行时间超过了定时时间（1ms）*/
   /*置TimerErrorFlag为1，表示定时中断错误*/
   TimerErrorFlag = 1;

   /*清标志位，避免中断连续触发，导致主函数完全无法执行*/
   TIM_ClearITPendingBit(TIM1, TIM_IT_Update);
  }

  /*中断函数退出前，读取计数器的值，此值可用于测量中断函数的具体执行时间*/
   Timercount0= TIM_GetCounter(TIM1);
 }
}

