"use client";

import {useEffect,useMemo,useState} from 'react';
import {loadRuntimeLogs,type RuntimeLog} from '@/lib/cloud-api';

export function RuntimeLogsPanel({taskId,groupId}:{taskId?:number;groupId?:number}){
 const [logs,setLogs]=useState<RuntimeLog[]>([]),[error,setError]=useState(''),[query,setQuery]=useState(''),[onlyProblems,setOnlyProblems]=useState(false),[loading,setLoading]=useState(false);
 async function refresh(){setLoading(true);try{setLogs(await loadRuntimeLogs(taskId,groupId));setError('')}catch(e){setError(e instanceof Error?e.message:'读取运行日志失败')}finally{setLoading(false)}}
 useEffect(()=>{void refresh()},[taskId,groupId]);
 const visible=useMemo(()=>logs.filter(log=>{
  if(onlyProblems&&!/(FAIL|ERROR|UNCERTAIN|BLOCKED|SUSPECT)/.test(log.type))return false;
  const text=[log.type,log.detail,log.title,log.group_label,log.printer_name,String(log.cloud_id??'')].join(' ');
  return text.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
 }),[logs,query,onlyProblems]);
 function download(){
  const payload={exported_at:new Date().toISOString(),task_id:taskId??null,group_id:groupId??null,logs:visible};
  const url=URL.createObjectURL(new Blob([JSON.stringify(payload,null,2)],{type:'application/json'}));
  const a=document.createElement('a');a.href=url;a.download=`print-runtime-${Date.now()}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
 }
 return <section className="panel runtime-logs-panel"><div className="panel-head"><h2>运行日志</h2><span className="muted">{taskId?`任务 #${taskId}${groupId?` · 班级作业 #${groupId}`:''}`:'本机打印站'}</span><button className="text-button head-end" disabled={loading} onClick={()=>void refresh()}>{loading?'刷新中…':'刷新'}</button><button className="text-button" disabled={!visible.length} onClick={download}>导出诊断记录</button></div><div className="runtime-logs-tools"><input aria-label="搜索运行日志" placeholder="搜索任务、班级、打印机或错误" value={query} onChange={e=>setQuery(e.target.value)}/><label><input type="checkbox" checked={onlyProblems} onChange={e=>setOnlyProblems(e.target.checked)}/> 只看异常</label><span>{visible.length} 条</span></div>{error&&<p role="alert" className="station-notice">{error}</p>}<div className="runtime-logs-list">{visible.map(log=><article key={log.id} className={'runtime-log '+(/(FAIL|ERROR|UNCERTAIN|BLOCKED|SUSPECT)/.test(log.type)?'problem':'')}><time>{new Date(log.ts*1000).toLocaleString('zh-CN')}</time><strong>{log.type}</strong><span>{log.title?`任务 #${log.cloud_id} · ${log.title}`:'打印站'}{log.group_label?` · ${log.group_label}`:''}{log.printer_name?` · ${log.printer_name}`:''}</span><p>{log.detail}</p></article>)}{!visible.length&&!loading&&<p className="hint">没有符合条件的记录</p>}</div><p className="hint">日志反映程序与 Windows 队列能读取到的状态；请结合打印机屏幕错误和实际出纸核对。</p></section>;
}
