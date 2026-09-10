import type { Device, Job, Preset, State } from "@/lib/print-model";

type CloudTask = {
  id:number; title:string; school:string; grade:string; classes:number; copies:number;
  copies_per_class:number; pages:number; source_user:string; status:string;
  created_at:number; done_at:number|null;
};
type Snapshot = {
  ok:boolean; error?:string; now:number; user:{username:string;role:string};
  tasks:CloudTask[]; presets:Preset[]; printers:Device[];
};

const apiBase = typeof window !== "undefined" && window.location.hostname === "printer.7988798.xyz"
  ? "" : "https://printer.7988798.xyz";

async function request(path:string, init?:RequestInit):Promise<Snapshot>{
  const response=await fetch(apiBase+path,{credentials:"include",...init});
  let body:Snapshot;
  try{body=await response.json() as Snapshot}catch{throw new Error(`云端返回异常（HTTP ${response.status}），请重新登录后再试`)}
  if(!response.ok||!body.ok)throw new Error(body.error||"云端请求失败");
  return body;
}
const iso=(seconds:number|null|undefined)=>new Date((seconds||Date.now()/1000)*1000).toISOString();
const statuses:Record<string,string>={QUEUED:"排队中",PRINTING:"打印中",AWAITING_PICKUP:"待取件",DONE:"已完成",CANCELED:"已取消"};

export function toState(data:Snapshot):State{
  const username=data.user.username;
  const jobs:Job[]=data.tasks.map(task=>({
    id:task.id,title:task.title,school:task.school,grade:task.grade,classes:task.classes,
    copies:task.copies_per_class||task.copies,pages:task.pages,user:task.source_user,
    status:statuses[task.status]||task.status,progress:task.status==="DONE"?100:0,
    device:null,deviceName:"—",allocations:[],local:task.source_user===username,
    createdAt:iso(task.created_at),completedAt:task.done_at?iso(task.done_at):null,
    duplex:"双面",color:"黑白",
  }));
  return {jobs,devices:data.printers||[],presets:data.presets||[],clock:iso(data.now),paused:false,logs:[]};
}

export const loadSnapshot=()=>request("/api/v1/bootstrap");
export const saveSchool=(id:number|null,input:Omit<Preset,"id">)=>request(
  id===null?"/api/v1/schools":`/api/v1/schools/${id}`,{
    method:id===null?"POST":"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(input),
  });
export const deleteSchool=(id:number)=>request(`/api/v1/schools/${id}`,{method:"DELETE"});
export const cancelTask=(id:number)=>request(`/api/v1/tasks/${id}/cancel`,{method:"POST"});
export const uploadTask=(input:{file:File;school:string;grade:string;classes:number;copies:number})=>{
  const form=new FormData();
  form.set("file",input.file);form.set("school",input.school);form.set("grade",input.grade);
  form.set("classes",String(input.classes));form.set("copies",String(input.copies));
  return request("/api/v1/tasks",{method:"POST",body:form});
};
