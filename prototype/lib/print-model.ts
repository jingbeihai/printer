export type SpecialCopies={classes:string;copies:number};
export type Preset={id:number;school:string;grade:string;classes:number;copies:number;special_copies:SpecialCopies[]};
export function classCopies(classes:number,defaultCopies:number,rules:SpecialCopies[]):number[]{
 if(!Number.isInteger(classes)||classes<1||classes>10000||!Number.isInteger(defaultCopies)||defaultCopies<1||defaultCopies>10000)throw new Error('班级数和份数必须是 1 到 10000 的整数');
 const values=Array(classes).fill(defaultCopies) as number[],seen=new Set<number>();
 for(const rule of rules){
  if(!Number.isInteger(rule.copies)||rule.copies<1||rule.copies>10000)throw new Error('特殊班级份数必须是 1 到 10000 的整数');
  const tokens=rule.classes.trim().split(/[,，、\s]+/);
  if(!rule.classes.trim())throw new Error('请填写特殊班级');
  for(const token of tokens){
   if(!/^\d+(?:-\d+)?$/.test(token))throw new Error(`班级范围格式无效：${token}`);
   const [start,end=start]=token.split('-').map(Number);
   if(start<1||end>classes||start>end)throw new Error(`班级范围超出 1 到 ${classes} 班：${token}`);
   for(let i=start;i<=end;i++){if(seen.has(i))throw new Error(`第 ${i} 班重复设置`);seen.add(i);values[i-1]=rule.copies}
  }
 }
 return values;
}
export type Device={id:number;name:string;ip:string;queue:string;state:string;minutes:number;total:number;printed:number;limit:number;rest:number;left:number;mode:string};
export type Allocation={deviceId:number;deviceName:string;copies:number;pages:number};
export type Job={id:number;title:string;school:string;grade:string;classes:number;copies:number;totalCopies:number;classCopies:number[];coverEnabled:boolean;sheets:number;pages:number;user:string;status:string;progress:number;device:number|null;deviceName:string;allocations:Allocation[];local:boolean;pickable?:boolean;createdAt:string;completedAt:string|null;duplex:string;color:string};
export type State={devices:Device[];jobs:Job[];presets:Preset[];clock:string;paused:boolean;logs:string[]};
export const totalPages=(j:Pick<Job,"pages"|"totalCopies">)=>j.pages*j.totalCopies;
export const dayKey=(date:string)=>new Date(new Date(date).getTime()+8*3600000).toISOString().slice(0,10);
export const clockLabel=(date:string)=>new Date(new Date(date).getTime()+8*3600000).toISOString().slice(0,16).replace("T"," ");
export function completedStats(jobs:Job[],date:string,period:"day"|"month"){
 const key=dayKey(date).slice(0,period==="day"?10:7);
 const rows=jobs.filter(j=>j.status==="已完成"&&j.completedAt&&dayKey(j.completedAt).startsWith(key)).sort((a,b)=>b.completedAt!.localeCompare(a.completedAt!));
 return {rows,tasks:rows.length,pages:rows.reduce((sum,j)=>sum+totalPages(j),0),
  sheets:rows.reduce((sum,j)=>sum+j.sheets,0),coverSheets:rows.reduce((sum,j)=>sum+(j.coverEnabled?j.classes:0),0)};
}
