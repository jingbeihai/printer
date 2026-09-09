export type Preset={id:number;school:string;grade:string;classes:number;copies:number};
export type Device={id:number;name:string;ip:string;queue:string;state:string;minutes:number;total:number;printed:number;limit:number;rest:number;left:number;mode:string};
export type Allocation={deviceId:number;deviceName:string;copies:number;pages:number};
export type Job={id:number;title:string;school:string;grade:string;classes:number;copies:number;pages:number;user:string;status:string;progress:number;device:number|null;deviceName:string;allocations:Allocation[];local:boolean;createdAt:string;completedAt:string|null;duplex:string;color:string};
export type State={devices:Device[];jobs:Job[];presets:Preset[];clock:string;paused:boolean;logs:string[]};
export const totalPages=(j:Pick<Job,"pages"|"classes"|"copies">)=>j.pages*j.classes*j.copies;
export const dayKey=(date:string)=>new Date(new Date(date).getTime()+8*3600000).toISOString().slice(0,10);
export const clockLabel=(date:string)=>new Date(new Date(date).getTime()+8*3600000).toISOString().slice(0,16).replace("T"," ");
export function createInitialState():State{
 const clock="2026-09-09T01:42:00.000Z";
 const presets=[{id:1,school:"实验小学",grade:"三年级",classes:3,copies:40},{id:2,school:"实验小学",grade:"四年级",classes:4,copies:45},{id:3,school:"中心小学",grade:"五年级",classes:2,copies:40},{id:4,school:"育才小学",grade:"六年级",classes:2,copies:30}];
 const jobs:Job[]=[
 [1008,"数学 · 单元练习",0,4,"陈老师","打印中",62,1,"2026-09-09T01:12:00Z",null],
 [1009,"英语 · 阅读训练",1,6,"我","排队中",0,null,"2026-09-09T01:18:00Z",null],
 [1010,"语文 · 期中复习",2,8,"刘老师","排队中",0,null,"2026-09-09T01:24:00Z",null],
 [1011,"数学 · 随堂练习",3,2,"赵老师","排队中",0,null,"2026-09-09T01:31:00Z",null],
 [1007,"语文 · 阅读材料",0,4,"我","已完成",100,2,"2026-09-09T00:20:00Z","2026-09-09T00:46:00Z"],
 [1006,"英语 · 单词练习",1,2,"王老师","已完成",100,4,"2026-09-09T00:00:00Z","2026-09-09T00:35:00Z"],
 [1005,"数学 · 周末练习",2,6,"刘老师","已完成",100,3,"2026-09-08T00:10:00Z","2026-09-08T01:10:00Z"],
 [1004,"语文 · 开学测评",3,8,"赵老师","已完成",100,2,"2026-09-03T00:10:00Z","2026-09-03T01:10:00Z"],
 [1003,"暑期 · 阅读练习",0,2,"我","已完成",100,1,"2026-08-31T00:10:00Z","2026-08-31T01:10:00Z"],
 ].map(row=>{const [id,title,p,pages,user,status,progress,device,createdAt,completedAt]=row;const preset=presets[Number(p)],deviceId=device===null?null:Number(device),deviceName=deviceId===null?"—":String(deviceId).padStart(2,"0")+" 号机";return {title:String(title),...preset,pages:Number(pages),user:String(user),status:String(status),progress:Number(progress),device:deviceId,deviceName,allocations:deviceId===null?[]:[{deviceId,deviceName,copies:preset.classes*preset.copies,pages:Number(pages)*preset.classes*preset.copies}],local:user==="我",createdAt:String(createdAt),completedAt:completedAt===null?null:String(completedAt),duplex:"双面",color:"黑白",...{id:Number(id)}}});
 const devices:Device[]=[106,52,120,28,0].map((minutes,i)=>({id:i+1,name:String(i+1).padStart(2,"0")+" 号机",ip:"192.168.1."+String(101+i),queue:"Canon C5560 "+String(i+1),state:["打印中","空闲","休息中","空闲","离线"][i],minutes,total:[196,142,240,118,86][i],printed:[1297,1840,2400,1560,680][i],limit:i===1?180:120,rest:30,left:i===2?18:0,mode:"自动休息"}));
 return {clock,presets,jobs,devices,paused:false,logs:["09:42 · 03 号机进入计划休息","09:38 · 05 号机连接中断"]};
}
export function completedStats(jobs:Job[],date:string,period:"day"|"month"){
 const key=dayKey(date).slice(0,period==="day"?10:7);
 const rows=jobs.filter(j=>j.status==="已完成"&&j.completedAt&&dayKey(j.completedAt).startsWith(key)).sort((a,b)=>b.completedAt!.localeCompare(a.completedAt!));
 return {rows,tasks:rows.length,pages:rows.reduce((sum,j)=>sum+totalPages(j),0)};
}
export function resolvePreset(presets:Preset[],school:string,grade?:string){return presets.find(p=>p.school===school&&(!grade||p.grade===grade))}
export function addPreset(state:State,input:Omit<Preset,"id">):State{
 const school=input.school.trim(),grade=input.grade.trim();
 if(!school||!grade||![input.classes,input.copies].every(n=>Number.isInteger(n)&&n>0&&n<=10000))throw new Error("请填写学校、年级以及有效的班级数和每班份数");
 if(state.presets.some(p=>p.school===school&&p.grade===grade))throw new Error("该学校的年级已存在，请勿重复创建");
 return {...state,presets:[...state.presets,{...input,school,grade,id:Math.max(0,...state.presets.map(p=>p.id))+1}]};
}
export function updatePreset(state:State,id:number,input:Omit<Preset,"id">):State{
 const school=input.school.trim(),grade=input.grade.trim();
 if(!school||!grade||![input.classes,input.copies].every(n=>Number.isInteger(n)&&n>0&&n<=10000))throw new Error("请填写学校、年级以及有效的班级数和每班份数");
 if(state.presets.some(p=>p.id!==id&&p.school===school&&p.grade===grade))throw new Error("该学校的年级已存在，请勿重复创建");
 if(!state.presets.some(p=>p.id===id))throw new Error("学校年级预设不存在");
 return {...state,presets:state.presets.map(p=>p.id===id?{id,school,grade,classes:input.classes,copies:input.copies}:p)};
}
export function removePreset(state:State,id:number):State{
 if(!state.presets.some(p=>p.id===id))throw new Error("学校年级预设不存在");
 return {...state,presets:state.presets.filter(p=>p.id!==id)};
}
export function removeDevice(state:State,id:number):State{
 if(state.jobs.some(j=>j.allocations.some(a=>a.deviceId===id)&&["打印中","待取件"].includes(j.status)))throw new Error("该打印机仍有未结束任务，请完成并确认取件后再删除");
 return {...state,devices:state.devices.filter(d=>d.id!==id)};
}
export function startJob(state:State,id:number,deviceIds:number[]):State{
 if(state.paused)throw new Error("请先恢复派单");
 const job=state.jobs.find(j=>j.id===id);if(!job||job.status!=="排队中")throw new Error("该任务不可开始打印");
 if(state.jobs.some(j=>["打印中","待取件"].includes(j.status)))throw new Error("请先完成当前任务并确认取件");
 const ids=[...new Set(deviceIds)];if(!ids.length)throw new Error("请至少勾选一台打印机");
 const selected=ids.map(id=>state.devices.find(d=>d.id===id));
 if(selected.some(d=>!d||d.state!=="空闲"||(d.mode!=="仅提醒"&&d.minutes>=d.limit)))throw new Error("所选打印机中有设备当前不可用，请重新选择");
 const total=job.classes*job.copies,base=Math.floor(total/ids.length),extra=total%ids.length;
 const allocations=selected.map((d,index)=>({deviceId:d!.id,deviceName:d!.name,copies:base+(index<extra?1:0),pages:(base+(index<extra?1:0))*job.pages}));
 const names=selected.map(d=>d!.name).join("、");
 return {...state,devices:state.devices.map(d=>ids.includes(d.id)?{...d,state:"打印中"}:d),jobs:state.jobs.map(j=>j.id===id?{...j,status:"打印中",device:ids[0],deviceName:names,allocations}:j)};
}
export function advance(state:State):State{
 const clock=new Date(new Date(state.clock).getTime()+5*60000).toISOString();
 const devices=state.devices.map(d=>({...d}));const newlyResting:number[]=[];
 const jobs=state.jobs.map(j=>{
  if(j.status!=="打印中")return j;
  const assigned=j.allocations.map(a=>devices.find(d=>d.id===a.deviceId)).filter((d):d is Device=>Boolean(d));if(!assigned.length||assigned.some(d=>d.state==="离线"))return j;
  const progress=Math.min(100,j.progress+20);
  assigned.forEach(d=>{const allocation=j.allocations.find(a=>a.deviceId===d.id)!;d.minutes+=5;d.total+=5;d.printed+=Math.floor(allocation.pages*progress/100)-Math.floor(allocation.pages*j.progress/100);if(progress===100){d.state=d.mode==="自动休息"&&d.minutes>=d.limit?"休息中":"空闲";if(d.state==="休息中"){d.left=d.rest;newlyResting.push(d.id)}}});
  return {...j,progress,status:progress===100?"待取件":"打印中"};
 });
 devices.forEach(d=>{if(d.state==="休息中"&&!newlyResting.includes(d.id)){d.left=Math.max(0,d.left-5);if(d.left===0){d.minutes=0;d.state="空闲"}}});
 return {...state,jobs,devices,clock};
}
export function completeJob(state:State,id:number):State{
 const job=state.jobs.find(j=>j.id===id);if(!job||job.status!=="待取件")throw new Error("任务尚未打印完成");
 return {...state,jobs:state.jobs.map(j=>j.id===id?{...j,status:"已完成",completedAt:state.clock}:j)};
}
export function cancelJob(state:State,id:number):State{
 const job=state.jobs.find(j=>j.id===id);if(!job||["已完成","已取消"].includes(job.status))throw new Error("该任务不能取消");
 const ids=job.allocations.map(a=>a.deviceId);
 return {...state,devices:state.devices.map(d=>ids.includes(d.id)&&d.state==="打印中"?{...d,state:"空闲"}:d),jobs:state.jobs.map(j=>j.id===id?{...j,status:"已取消",progress:0}:j)};
}
