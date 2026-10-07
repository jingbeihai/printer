import type { Device, Job, Preset, State } from "@/lib/print-model";

type CloudTask = {
  id:number; title:string; school:string; grade:string; classes:number; copies:number;
  copies_per_class:number; pages:number; source_user:string; status:string;
  created_at:number; done_at:number|null;
  claimed?:boolean; print_requested_at?:number;
};
export type Snapshot = {
  ok:boolean; error?:string; now:number; user:{username:string;role:string};
  client_kind?:"desktop"|"web";
  tasks:CloudTask[]; presets:Preset[]; printers:Device[];
  station_available?:boolean; station_ready?:boolean; station_sync_error?:string;
  station_pending_print_ids?:number[];
  station_pickup_ids?:number[];
};

const apiBase = "";

async function request(path:string, init?:RequestInit):Promise<Snapshot>{
  const response=await fetch(apiBase+path,{credentials:"include",...init});
  let body:Snapshot;
  try{body=await response.json() as Snapshot}catch{throw new Error(`云端返回异常（HTTP ${response.status}），请重新登录后再试`)}
  if(response.status===401&&typeof window!=="undefined")window.location.assign("/login");
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
    status:task.status==="QUEUED"&&(!!task.claimed||data.station_pending_print_ids?.includes(task.id))
      ?"待下发":statuses[task.status]||task.status,progress:task.status==="DONE"?100:0,
    device:null,deviceName:"—",allocations:[],local:task.source_user===username,
    pickable:data.station_pickup_ids?.includes(task.id)||false,
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
export const moveTask=(id:number,direction:"up"|"down")=>request(`/api/v1/tasks/${id}/move`,{
  method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({direction}),
});
export type ManagedUser={id:number;username:string;role:"user"|"superadmin";active:number;display_name:string;created_at:number};
export type AdminOverview={ok:boolean;error?:string;users:ManagedUser[];schools:Preset[];invite_code:string};
async function adminRequest(path:string,init?:RequestInit):Promise<AdminOverview>{
  const response=await fetch(apiBase+path,{credentials:"include",...init});
  const body=await response.json() as AdminOverview;
  if(response.status===401&&typeof window!=="undefined")window.location.assign("/login");
  if(!response.ok||!body.ok)throw new Error(body.error||"管理操作失败");
  return body;
}
const jsonBody=(value:unknown):RequestInit=>({headers:{"Content-Type":"application/json"},body:JSON.stringify(value)});
export const loadAdmin=()=>adminRequest("/api/v1/admin/overview");
export const createManagedUser=(value:{username:string;password:string;role:ManagedUser["role"]})=>adminRequest("/api/v1/admin/users",{method:"POST",...jsonBody(value)});
export const updateManagedUser=(id:number,value:{role:ManagedUser["role"];active:boolean;password?:string})=>adminRequest(`/api/v1/admin/users/${id}`,{method:"PUT",...jsonBody(value)});
export const deleteManagedUser=(id:number)=>adminRequest(`/api/v1/admin/users/${id}`,{method:"DELETE"});
export const updateInviteCode=(invite_code:string)=>adminRequest("/api/v1/admin/invite",{method:"PUT",...jsonBody({invite_code})});
export type Account={username:string;role:"user"|"superadmin";display_name:string};
async function accountRequest(init?:RequestInit):Promise<{ok:boolean;error?:string;account:Account}>{
  const response=await fetch(apiBase+"/api/v1/account",{credentials:"include",...init});
  const body=await response.json() as {ok:boolean;error?:string;account:Account};
  if(response.status===401&&typeof window!=="undefined")window.location.assign("/login");
  if(!response.ok||!body.ok)throw new Error(body.error||"用户中心操作失败");
  return body;
}
export const loadAccount=()=>accountRequest();
export const updateAccount=(value:{display_name:string;old_password?:string;new_password?:string})=>accountRequest({method:"PUT",...jsonBody(value)});
export type FleetTask={id:number;title:string;status:string;paused:boolean;printer_ids:number[];active_printer_ids:number[];completed_groups:number;total_groups:number};
type LocalReply={ok:boolean;error?:string;message?:string;tasks?:FleetTask[];printers?:{queue:string;driver:string;port:string}[]};
async function localRequest(path:string,init?:RequestInit):Promise<LocalReply>{
  const response=await fetch(path,{credentials:"include",...init});
  let body:LocalReply;
  try{body=await response.json() as LocalReply}catch{throw new Error(`本机打印站返回异常（HTTP ${response.status}）`)}
  if(response.status===401){window.location.assign("/login");throw new Error("登录已失效")}
  if(!response.ok||!body.ok)throw new Error(body.error||"本机打印站操作失败");
  return body;
}
const jsonPost=(body:unknown):RequestInit=>({method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
export const installedPrinters=async()=> (await localRequest("/desktop/printers/available")).printers||[];
export const addPrinter=(input:{name:string;queue:string;ip:string})=>localRequest("/desktop/printers",jsonPost(input));
export const deletePrinter=(id:number)=>localRequest(`/desktop/printers/${id}`,{method:"DELETE"});
export const loadFleet=async()=> (await localRequest('/desktop/fleet')).tasks||[];
export const controlFleetTask=(id:number,action:'pause'|'resume'|'cancel'|'up'|'down',printer_id?:number)=>localRequest(`/desktop/tasks/${id}/control`,jsonPost({action,printer_id}));
export const printTask=(id:number,printerIds:number[])=>localRequest(`/desktop/tasks/${id}/print`,jsonPost({printer_ids:printerIds}));
export const confirmPickup=(id:number)=>localRequest(`/desktop/tasks/${id}/pickup`,{method:"POST"});
type UploadReply={ok:boolean;task_id?:number;storage_backend?:"local";upload_url?:string;error?:string};
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
    xhr.onerror=()=>reject(new Error("无法直连文件存储，可能是网络或跨域设置问题"));
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
  if(prepared.storage_backend==="local"){
    const form=new FormData();
    form.set("file",input.file);form.set("school",input.school);form.set("grade",input.grade);
    form.set("classes",String(input.classes));form.set("copies",String(input.copies));
    form.set("pages",String(input.pages));
    const result=await uploadRequest("/api/v1/tasks",{method:"POST",body:form});
    onProgress?.(100);
    return result;
  }
  if(!prepared.upload_url||!prepared.task_id)throw new Error("云端未返回 COS 上传地址");
  try{
    try{
      await putToCos(prepared.upload_url,input.file,onProgress);
    }catch{
      // Reuse the reserved task and object, even if COS accepted the PUT but
      // the browser could not read its response. Never create a second task.
      onProgress?.(0);
      const form=new FormData();form.set("file",input.file);
      await uploadRequest(`/api/v1/uploads/${prepared.task_id}/file`,{method:"POST",body:form});
    }
    onProgress?.(100);
    return await uploadRequest(`/api/v1/uploads/${prepared.task_id}/complete`,{method:"POST"});
  }catch(error){
    void fetch(apiBase+`/api/v1/uploads/${prepared.task_id}`,{method:"DELETE",credentials:"include"}).catch(()=>{});
    throw error;
  }
};
