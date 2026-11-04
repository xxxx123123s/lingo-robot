#ifndef __PID_H
#define __PID_H

typedef struct {
	float Target;
	float Actual;
	float Actual1;	//上一拍的测量值，用于"只对测量值求导"，避免目标突变时的微分冲击
	float Out;
	
	float Kp;
	float Ki;
	float Kd;
	
	float Error0;
	float Error1;
	float ErrorInt;
	
	float OutMax;
	float OutMin;
	
	float IntMax;	//积分项最多能贡献多少输出（和 Out 同单位）。填 0 则退回用 PID_INT_RATIO 估算
	
	/*外部微分输入（单位：测量值/秒），UseDIn=1 时用它代替 (Actual1-Actual)。
	  用途：角度环的 D 项直接用陀螺仪输出的角速度。
	  对融合角做差分的等效增益 = Kd*0.01，而滤波后的角度噪声会被放大 100 倍，
	  所以差分法根本给不出有效阻尼；陀螺仪测的就是角速度，天然干净。*/
	float DIn;
	uint8_t UseDIn;
} PID_t;
void PID_init(PID_t *p);
void PID_Update(PID_t *p);

#endif
