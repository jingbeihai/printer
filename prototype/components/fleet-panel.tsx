"use client";
import {useEffect,useState} from 'react';
import type {Device} from '@/lib/print-model';
import {controlFleetTask,loadFleet,type FleetTask} from '@/lib/cloud-api';

export function FleetPanel({devices,notify,onChange,onAdd,onDelete}:{devices:Device[];notify:(text:string)=>void;onChange:()=>Promise<void>;onAdd:()=>void;onDelete:(id:number)=>void}){
 const [selected,setSelected]=useState<number|null>(null),[tasks,setTasks]=useState<FleetTask[]>([]),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const device=devices.find(d=>d.id===selected)||devices[0];
 useEffect(()=>{let active=true;let timer:ReturnType<typeof setTimeout>;async function poll(){try{const data=await loadFleet();if(active){setTasks(data);setError('')}}catch(e){if(active)setError(e instanceof Error?e.message:'读取机队任务失败')}finally{if(active)timer=setTimeout(poll,3000)}}void poll();return()=>{active=false;clearTimeout(timer)}},[]);
 async function act(task:FleetTask,action:'pause'|'resume'|'cancel'|'up'|'down'){
  if(busy)return;
  if(action==='cancel'&&!window.confirm(`取消《${task.title}》在所有已选打印机上的任务？已进入设备缓存的页面可能继续出纸。`))return;
  setBusy(true);
  try{const reply=await controlFleetTask(task.id,action,device?.id);notify(reply.message||({pause:'任务已暂停',resume:'任务已继续',cancel:'本机任务已取消',up:'任务已上移',down:'任务已下移'}[action]));setTasks(await loadFleet());await onChange()}catch(e){notify(e instanceof Error?e.message:'操作失败');try{setTasks(await loadFleet())}catch{}}finally{setBusy(false)}
 }
 const related=tasks.filter(t=>device&&t.printer_ids.includes(device.id));
 const current=related.filter(t=>t.active_printer_ids.includes(device?.id??-1));
 const waiting=related.filter(t=>!t.active_printer_ids.includes(device?.id??-1)&&t.status!=='AWAITING_PICKUP');
 const pickup=related.filter(t=>t.status==='AWAITING_PICKUP');
 function card(t:FleetTask,queued:boolean){return <article className="fleet-task" key={t.id}><div className="fleet-task-heading"><strong>{t.title}</strong><span className="badge">{t.paused?'已暂停':queued?'等待下发':'执行中'}</span></div><p>任务 #{t.id} · 已完成 {t.completed_groups} / {t.total_groups} 组{t.printer_ids.length>1?' · 多台打印机共同执行':''}</p><div className="row-actions"><button className="text-button" disabled={busy} onClick={()=>void act(t,t.paused?'resume':'pause')}>{t.paused?'继续':'暂停'}</button><button className="text-button danger" disabled={busy} onClick={()=>void act(t,'cancel')}>取消任务</button>{queued&&<><button className="text-button" disabled={busy||!!t.active_printer_ids.length||waiting.filter(x=>!x.active_printer_ids.length)[0]?.id===t.id} onClick={()=>void act(t,'up')}>上移</button><button className="text-button" disabled={busy||!!t.active_printer_ids.length||waiting.filter(x=>!x.active_printer_ids.length).at(-1)?.id===t.id} onClick={()=>void act(t,'down')}>下移</button></>}</div></article>}
 return <div className="fleet-layout"><aside className="panel fleet-devices"><div className="panel-head"><h2>打印机</h2><button className="primary head-end" onClick={onAdd}>添加打印机</button></div><div className="fleet-device-list">{devices.map(d=><button key={d.id} className={'fleet-device '+(device?.id===d.id?'selected':'')} onClick={()=>setSelected(d.id)}><strong>{d.name}</strong><span>{d.state} · {tasks.filter(t=>t.printer_ids.includes(d.id)&&!t.active_printer_ids.includes(d.id)&&t.status!=='AWAITING_PICKUP').length} 项等待</span><small>{tasks.find(t=>t.active_printer_ids.includes(d.id))?.title||'暂无执行任务'}</small></button>)}{!devices.length&&<p className="hint">请先添加打印机</p>}</div></aside><section className="panel fleet-detail"><div className="panel-head"><h2>{device?.name||'机队任务'}</h2>{device&&<button className="text-button danger head-end" onClick={()=>onDelete(device.id)}>删除打印机</button>}</div><div className="fleet-task-list">{error&&<p role="alert" className="station-notice">{error}</p>}{device&&<p className="hint">{device.queue} · {device.ip||'未设置 IP'} · 累计 {device.printed} 张</p>}<p className="hint">暂停、继续、取消作用于整项任务。已进入打印机缓存的页面可能继续出纸。</p><h3>正在执行</h3>{current.map(t=>card(t,false))}{!current.length&&<p className="hint">暂无正在执行的任务</p>}<h3>等待任务 · 按执行顺序</h3>{waiting.map(t=>card(t,true))}{!waiting.length&&<p className="hint">暂无等待任务</p>}{!!pickup.length&&<><h3>等待收纸</h3>{pickup.map(t=><p key={t.id} className="fleet-task">{t.title} · 请到任务列表确认收纸</p>)}</>}</div></section></div>
}
