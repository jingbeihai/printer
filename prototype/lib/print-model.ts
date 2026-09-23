export type Preset={id:number;school:string;grade:string;classes:number;copies:number};
export type Device={id:number;name:string;ip:string;queue:string;state:string;minutes:number;total:number;printed:number;limit:number;rest:number;left:number;mode:string};
export type Allocation={deviceId:number;deviceName:string;copies:number;pages:number};
export type Job={id:number;title:string;school:string;grade:string;classes:number;copies:number;pages:number;user:string;status:string;progress:number;device:number|null;deviceName:string;allocations:Allocation[];local:boolean;pickable?:boolean;createdAt:string;completedAt:string|null;duplex:string;color:string};
export type State={devices:Device[];jobs:Job[];presets:Preset[];clock:string;paused:boolean;logs:string[]};
export const totalPages=(j:Pick<Job,"pages"|"classes"|"copies">)=>j.pages*j.classes*j.copies;
export const dayKey=(date:string)=>new Date(new Date(date).getTime()+8*3600000).toISOString().slice(0,10);
export const clockLabel=(date:string)=>new Date(new Date(date).getTime()+8*3600000).toISOString().slice(0,16).replace("T"," ");
export function completedStats(jobs:Job[],date:string,period:"day"|"month"){
 const key=dayKey(date).slice(0,period==="day"?10:7);
 const rows=jobs.filter(j=>j.status==="已完成"&&j.completedAt&&dayKey(j.completedAt).startsWith(key)).sort((a,b)=>b.completedAt!.localeCompare(a.completedAt!));
 return {rows,tasks:rows.length,pages:rows.reduce((sum,j)=>sum+totalPages(j),0)};
}
