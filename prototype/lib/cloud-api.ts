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
type UploadReply={ok:boolean;task_id:number;upload_url?:string;error?:string};
async function uploadRequest(path:string,init:RequestInit):Promise<UploadReply>{
  const response=await fetch(apiBase+path,{credentials:"include",...init});
  let body:UploadReply;
  try{body=await response.json()}catch{throw new Error(`云端返回异常（HTTP ${response.status}）`)}
  if(!response.ok||!body.ok)throw new Error(body.error||"上传失败");
  return body;
}
function putToCos(url:string,file:File,onProgress?:(percent:number)=>void){
  return new Promise<void>((resolve,reject)=>{
    const xhr=new XMLHttpRequest();xhr.open("PUT",url,true);xhr.timeout=30*60*1000;
    xhr.setRequestHeader("Content-Type","application/pdf");
    xhr.upload.onprogress=event=>{if(event.lengthComputable)onProgress?.(Math.round(event.loaded/event.total*100))};
    xhr.onload=()=>xhr.status>=200&&xhr.status<300?resolve():reject(new Error(`COS 上传失败（HTTP ${xhr.status}）`));
    xhr.onerror=()=>reject(new Error("无法直连腾讯云 COS，请检查存储桶跨域 CORS 设置"));
    xhr.ontimeout=()=>reject(new Error("COS 上传超时，请检查网络后重试"));
    xhr.send(file);
  });
}
export const uploadTask=async (
  input:{file:File;school:string;grade:string;classes:number;copies:number;pages:number},
  onProgress?:(percent:number)=>void,
)=>{
  const prepared=await uploadRequest("/api/v1/uploads/presign",{
    method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({
      filename:input.file.name,size:input.file.size,school:input.school,grade:input.grade,
      classes:input.classes,copies:input.copies,pages:input.pages,
    }),
  });
  if(!prepared.upload_url)throw new Error("云端未返回 COS 上传地址");
  try{
    await putToCos(prepared.upload_url,input.file,onProgress);
    return await uploadRequest(`/api/v1/uploads/${prepared.task_id}/complete`,{method:"POST"});
  }catch(error){
    void fetch(apiBase+`/api/v1/uploads/${prepared.task_id}`,{method:"DELETE",credentials:"include"}).catch(()=>{});
    throw error;
  }
};
