"use client";
import {useEffect,useState} from 'react';
import type {Device} from '@/lib/print-model';
import {controlFleetTask,loadFleet,resolveFleetClass,type FleetTask} from '@/lib/cloud-api';

export function FleetPanel({devices,notify,onChange,onAdd,onDelete,onOpenLogs}:{devices:Device[];notify:(text:string)=>void;onChange:()=>Promise<void>;onAdd:()=>void;onDelete:(id:number)=>void;onOpenLogs?:(taskId:number,groupId?:number)=>void}){
 const [selected,setSelected]=useState<number|null>(null),[tasks,setTasks]=useState<FleetTask[]>([]),[busy,setBusy]=useState(false),[pendingAction,setPendingAction]=useState(''),[error,setError]=useState('');
 const device=devices.find(d=>d.id===selected)||devices[0];
 useEffect(()=>{let active=true;let timer:ReturnType<typeof setTimeout>;async function poll(){try{const data=await loadFleet();if(active){setTasks(data);setError('')}}catch(e){if(active)setError(e instanceof Error?e.message:'读取机队任务失败')}finally{if(active)timer=setTimeout(poll,3000)}}void poll();return()=>{active=false;clearTimeout(timer)}},[]);
 async function act(task:FleetTask,action:'pause'|'resume'|'cancel'){
  if(busy)return;
  if(action==='cancel'&&!window.confirm(`取消《${task.title}》在所有已选打印机上的任务？已进入设备缓存的页面可能继续出纸。`))return;
  setBusy(true);setPendingAction(`${action==='cancel'?'正在取消':action==='pause'?'正在暂停':'正在继续'}《${task.title}》：正在等待当前下发结束并核对 Windows 队列…`);
  try{const reply=await controlFleetTask(task.id,action,device?.id);notify(reply.message||({pause:'任务已暂停',resume:'任务已继续',cancel:'本机任务已取消'}[action]));setTasks(await loadFleet());await onChange()}catch(e){notify(e instanceof Error?e.message:'操作失败');try{setTasks(await loadFleet())}catch{}}finally{setBusy(false);setPendingAction('')}
 }
 async function resolve(task:FleetTask,groupId:number,action:'retry'){
  if(busy)return;
  const prompt='确认废弃本班已出纸张并整班重打？系统会先核对原打印机队列。';
  if(!window.confirm(prompt))return;
  setBusy(true);try{let reply;try{reply=await resolveFleetClass(task.id,groupId,action,action==='retry')}catch(e){if(action!=='retry'||!(e instanceof Error)||!e.message.includes('队列已空且设备空闲'))throw e;if(!window.confirm('未能精确匹配旧作业，Windows 队列已空且设备空闲。请确认旧作业不会继续出纸，并废弃本班任何已出纸张。仍要整班重新打印吗？')){notify('已取消重打，继续自动核对队列');return}reply=await resolveFleetClass(task.id,groupId,'retry',true,true)}notify(reply.message||'班级作业已更新');setTasks(await loadFleet());await onChange()}catch(e){notify(e instanceof Error?e.message:'班级作业处理失败')}finally{setBusy(false)}
 }
 const related=tasks.filter(t=>device&&t.printer_ids.includes(device.id));
 const current=related.filter(t=>t.active_printer_ids.includes(device?.id??-1));
 const waiting=related.filter(t=>!t.active_printer_ids.includes(device?.id??-1)&&t.status!=='AWAITING_PICKUP');
 const pickup=related.filter(t=>t.status==='AWAITING_PICKUP');
 function card(t:FleetTask,queued:boolean){
  const allocation=t.allocations.find(x=>x.printer_id===device?.id);
  const classes=t.class_groups.filter(g=>g.printer_id===device?.id);
  const names:Record<FleetTask['class_groups'][number]['display'],string>={pending:'待发送',submitting:'下发中',spooled:'已排队',paused:'已暂停',printing:'正在打印',done:'队列已处理',problem:'异常待处理'};
  return <article className="fleet-task" key={t.id}>
   <div className="fleet-task-heading"><strong>{t.title}</strong><span className="badge">{t.paused?'已暂停':queued?'等待下发':'执行中'}</span></div>
   <p>任务 #{t.id} · 总进度 {t.completed_groups}/{t.total_groups} 班 · 本机 {allocation?.completed||0}/{allocation?.groups||0} 班</p>
   <div className="fleet-progress-track" role="progressbar" aria-label="班级完成进度" aria-valuenow={t.completed_groups} aria-valuemin={0} aria-valuemax={t.total_groups}><span style={{width:`${t.total_groups?t.completed_groups/t.total_groups*100:0}%`}}/></div>
   <div className="fleet-class-grid">{classes.map(g=><button key={g.id} type="button" className={'fleet-class-tile '+g.display} onClick={()=>onOpenLogs?.(t.id,g.id)} title={`${g.label} · ${names[g.display]} · 查看日志`}><strong>{g.label}</strong><span>{names[g.display]}</span></button>)}</div>
   <div className="row-actions"><button className="text-button" disabled={busy} onClick={()=>void act(t,t.paused?'resume':'pause')}>{t.paused?'继续':'暂停'}</button><button className="text-button danger" disabled={busy} onClick={()=>void act(t,'cancel')}>取消任务</button><button className="text-button" onClick={()=>onOpenLogs?.(t.id)}>查看任务日志</button></div>
   {t.blocked_groups.filter(g=>g.printer_id===device?.id).map(g=><div className="fleet-task" key={g.id}><strong>{g.label} · 下发或打印异常</strong><div className="row-actions"><button className="text-button danger" disabled={busy} onClick={()=>void resolve(t,g.id,'retry')}>整班重新打印</button><button className="text-button" onClick={()=>onOpenLogs?.(t.id,g.id)}>查看日志</button></div></div>)}
  </article>
 }
 return <div className="fleet-layout"><aside className="panel fleet-devices"><div className="panel-head"><h2>打印机</h2><button className="primary head-end" onClick={onAdd}>添加打印机</button></div><div className="fleet-device-list">{devices.map(d=><button key={d.id} className={'fleet-device '+(device?.id===d.id?'selected':'')} onClick={()=>setSelected(d.id)}><strong>{d.name}</strong><span>{d.state} · {tasks.filter(t=>t.printer_ids.includes(d.id)&&!t.active_printer_ids.includes(d.id)&&t.status!=='AWAITING_PICKUP').length} 项等待</span><small>{tasks.find(t=>t.active_printer_ids.includes(d.id))?.title||'暂无执行任务'}</small></button>)}{!devices.length&&<p className="hint">请先添加打印机</p>}</div></aside><section className="panel fleet-detail"><div className="panel-head"><h2>{device?.name||'机队任务'}</h2>{device&&<button className="text-button danger head-end" onClick={()=>onDelete(device.id)}>删除打印机</button>}</div><div className="fleet-task-list">{error&&<p role="alert" className="station-notice">{error}</p>}{pendingAction&&<p role="status" className="station-notice">{pendingAction}</p>}{device&&<p className="hint">{device.queue} · {device.ip||'未设置 IP'} · 累计 {device.printed} 张</p>}<p className="hint">每台设备最多预排 3 班，按 Windows 队列处理结果自动完成；设备恢复后自动继续。暂停、取消后，设备缓存中的页面可能继续出纸。</p><h3>正在执行</h3>{current.map(t=>card(t,false))}{!current.length&&<p className="hint">暂无正在执行的任务</p>}<h3>等待任务 · 按执行顺序</h3>{waiting.map(t=>card(t,true))}{!waiting.length&&<p className="hint">暂无等待任务</p>}{!!pickup.length&&<><h3>等待收纸</h3>{pickup.map(t=><p key={t.id} className="fleet-task">{t.title} · 请到任务列表确认收纸</p>)}</>}</div></section></div>
}
