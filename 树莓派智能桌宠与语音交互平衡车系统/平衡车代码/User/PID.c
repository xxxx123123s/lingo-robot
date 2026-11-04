#include "stm32f10x.h"                  // Device header
#include "PID.h"

/* 抗积分饱和的兜底系数：只有在 PID_t 里没给 IntMax（也就是填 0）时才会用到。
   正常使用请直接在 main.c 里给每个环写 IntMax，单位就是"积分项最多贡献多少输出"，最直观。
   这里的 0.5 意思是"积分最多贡献半个量程"，对量程 100 的角度环来说就是 50 个 PWM，太松了。*/
#define PID_INT_RATIO 0.5f

void PID_init(PID_t *p)
{
	
	p->Actual = 0;
	p->Actual1 = 0;
	p->Out = 0;
	p->Error0 = 0;
	p->Error1 = 0;
	p->ErrorInt = 0;
}

void PID_Update(PID_t *p)
{
	float IntLimit;
	
	p->Error1 = p->Error0;
	p->Error0 = p->Target - p->Actual;
	
	if (p->Ki != 0)
	{
		/*抗饱和①：条件积分。上一拍输出已经顶到限幅、而这一拍误差还在往同方向推，
		  说明"再算也出不去"，此时停止累加，否则积分会一直长胖
		  （转向时转向环、平衡时角度环最容易在这里攒积分）*/
		if (! ((p->Out >= p->OutMax && p->Error0 > 0) ||
		       (p->Out <= p->OutMin && p->Error0 < 0)))
		{
			p->ErrorInt += p->Error0;
		}
		
		/*抗饱和②：积分限幅。积分项自己最多能贡献 IntMax 个输出量。
		  IntMax 由每个环单独指定（和 Out 同单位，最直观）：
		    角度环 12 PWM、速度环 3°、转向环 8 PWM。
		  为什么不能像以前那样统一给"半个量程"：角度环的量程是 100，半个量程就是 50 个 PWM，
		  等于允许积分攒出半个满输出 —— 前进行驶时外环给的前倾指令会被内环当成误差攒进去，
		  松摇杆时这堆积分瞬间变成多余的推力，就是"跑一会儿之后猛冲一下"。*/
		if (p->IntMax > 0)
		{
			IntLimit = p->IntMax / p->Ki;
		}
		else
		{
			IntLimit = PID_INT_RATIO * p->OutMax / p->Ki;
		}
		if (IntLimit < 0) {IntLimit = -IntLimit;}
		if (p->ErrorInt >  IntLimit) {p->ErrorInt =  IntLimit;}
		if (p->ErrorInt < -IntLimit) {p->ErrorInt = -IntLimit;}
	}
	else
	{
		p->ErrorInt = 0;
	}
	
	/*微分项只对【测量值】求导，不对目标值求导。
	  原来写的是 Kd*(Error0-Error1)，而 Error = Target - Actual，
	  所以 Target 一突变（松摇杆时 AnglePID.Target 从几度一拍跳到 0），
	  微分项就会打出一个 16~32 个 PWM 的尖峰，而且只持续一拍 ——
	  车上的表现就是"跑一会儿之后猛地窜一下"。这也是标准做法：
	  derivative on measurement，目标变化不再产生冲击，但阻尼作用一点不少。

	  UseDIn=1 时改用外部提供的微分输入（角度环用陀螺仪角速度）：
	  差分法 Kd*(Actual1-Actual) 的等效增益是 Kd×0.01/（°/s），要给足阻尼就得把
	  Kd 加到几十，那时角度噪声也被放大几十倍，输出直接打满乱抽。
	  陀螺仪输出的就是角速度，没有差分放大问题，Kd 可以按真实物理量来给。*/
	if (p->UseDIn)
	{
		p->Out = p->Kp * p->Error0
			   + p->Ki * p->ErrorInt
			   - p->Kd * p->DIn;
	}
	else
	{
		p->Out = p->Kp * p->Error0
			   + p->Ki * p->ErrorInt
			   + p->Kd * (p->Actual1 - p->Actual);
	}
	
	p->Actual1 = p->Actual;
	
	if (p->Out > p->OutMax) {p->Out = p->OutMax;}
	if (p->Out < p->OutMin) {p->Out = p->OutMin;}
}
