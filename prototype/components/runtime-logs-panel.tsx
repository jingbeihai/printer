"use client";

import {useEffect,useMemo,useState} from 'react';
import {loadRuntimeLogs,type RuntimeLog} from '@/lib/cloud-api';

const ISSUE=/(FAIL|ERROR|UNCERTAIN|BLOCKED|SUSPECT|UNOBSERVED|UNMATCHED)/;
const isProblem=(log:RuntimeLog)=>ISSUE.test(log.type+' '+log.detail)||/失败|异常|无法打印|需检查/.test(log.detail);
const labels:Record<string,string>={
 TASK_SYNCED:'任务已同步',TASK_DOWNLOADED:'文件已下载',TASK_STARTED:'任务已开始',TASK_DONE:'任务已完成',TASK_PICKED_UP:'已确认收纸',TASK_CANCELED:'任务已取消',
 PRINT_PREPARE_STARTED:'开始准备文件',PRINT_DOWNLOAD_DONE:'文件下载完成',PRINT_PREPARE_DONE:'文件准备完成',PRINT_REQUESTED:'已发起打印',CLASS_ALLOCATION:'班级已分配',
 SUBMIT_STARTED:'文件开始下发',SUBMIT_RETURNED:'文件已下发',SUBMIT_FAIL:'文件下发失败',SUBMIT_UNCERTAIN:'下发结果待核对',
 SPOOL_JOB_SEEN:'队列已接收',SPOOL_JOB_PRINTING:'正在打印',SPOOL_JOB_FAILED:'打印作业失败',SPOOL_UNOBSERVED:'完成结果待核对',SPOOL_MONITOR_ERROR:'队列监测异常',
 GROUP_ASSIGNED:'班级已分配',GROUP_DONE:'班级已完成',PRINTER_STATE:'设备状态变化',PRINTER_REST_STARTED:'设备开始休息',PRINTER_REST_FINISHED:'设备休息结束',
 TASK_CONTROL_REQUESTED:'已请求任务操作',TASK_CONTROL:'任务已操作',CLASS_MARKED_DONE:'班级已人工确认',CLASS_OLD_JOB_CANCELED:'旧作业已取消',CLASS_RETRY_UNMATCHED_MANUAL:'旧作业需人工核对',CLASS_RELEASED:'班级重新下发',
 PRINTER_ADDED:'已添加打印机',PRINTER_REMOVED:'已移除打印机',EXECUTOR_ERROR:'运行异常',
};
const eventName=(type:string)=>labels[type]||'运行记录';
const category=(type:string)=>{
 if(/^(SUBMIT|SPOOL|GROUP_)/.test(type))return '打印与队列';
 if(/^PRINTER_/.test(type))return '设备';
 if(/^CLASS_/.test(type))return '班级处理';
 if(/^TASK_|^PRINT_/.test(type))return '任务';
 return '其他';
};
const summary=(log:RuntimeLog)=>{
 if(log.type==='SPOOL_JOB_FAILED')return 'Windows 打印队列报告作业失败，请查看设备屏幕和打印队列。';
 if(log.type==='SPOOL_UNOBSERVED')return '文件已下发，但未能在 Windows 队列中确认作业。';
 if(log.type==='SUBMIT_FAIL')return '文件下发失败，系统将稍后重试。';
 if(log.type==='SUBMIT_UNCERTAIN')return '文件下发结果不确定，请核对打印队列，避免重复打印。';
 return log.detail||eventName(log.type);
};
const localDate=(value:number)=>new Date(value*1000).toLocaleDateString('zh-CN',{month:'2-digit',day:'2-digit'});
const localTime=(value:number)=>new Date(value*1000).toLocaleTimeString('zh-CN',{hour12:false});
const pageSize=20;

export function RuntimeLogsPanel({taskId,groupId,onClearScope}:{taskId?:number;groupId?:number;onClearScope?:()=>void}){
 const [logs,setLogs]=useState<RuntimeLog[]>([]),[error,setError]=useState(''),[query,setQuery]=useState('');
 const [onlyProblems,setOnlyProblems]=useState(false),[timeRange,setTimeRange]=useState('all'),[printer,setPrinter]=useState('all'),[eventType,setEventType]=useState('all');
 const [loading,setLoading]=useState(true),[page,setPage]=useState(1);
 async function refresh(){setLoading(true);try{setLogs(await loadRuntimeLogs(taskId,groupId));setError('')}catch(e){setError(e instanceof Error?e.message:'读取运行日志失败')}finally{setLoading(false)}}
 useEffect(()=>{void refresh()},[taskId,groupId]);
 const printers=useMemo(()=>Array.from(new Set(logs.map(log=>log.printer_name).filter((name):name is string=>Boolean(name)))).sort((a,b)=>a.localeCompare(b,'zh-CN')),[logs]);
 const visible=useMemo(()=>logs.filter(log=>{
  if(onlyProblems&&!isProblem(log))return false;
  if(printer!=='all'&&log.printer_name!==printer)return false;
  if(eventType!=='all'&&category(log.type)!==eventType)return false;
  const date=new Date(log.ts*1000);
  if(timeRange==='today'&&date.toDateString()!==new Date().toDateString())return false;
  if(timeRange==='week'&&log.ts*1000<Date.now()-7*86400000)return false;
  const text=[eventName(log.type),log.type,log.detail,log.title,log.group_label,log.printer_name,String(log.cloud_id??'')].join(' ');
  return text.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
 }),[logs,query,onlyProblems,timeRange,printer,eventType]);
 useEffect(()=>setPage(1),[query,onlyProblems,timeRange,printer,eventType,taskId,groupId]);
 const pages=Math.max(1,Math.ceil(visible.length/pageSize)),currentPage=Math.min(page,pages),pageRows=visible.slice((currentPage-1)*pageSize,currentPage*pageSize);
 function resetFilters(){setQuery('');setOnlyProblems(false);setTimeRange('all');setPrinter('all');setEventType('all');onClearScope?.()}
 return <section className="panel runtime-logs-panel">
  <div className="runtime-logs-filters">
   <div className="runtime-logs-filter-grid">
    <label className="runtime-log-search"><span>搜索</span><input aria-label="搜索运行日志" placeholder="搜索任务、班级、打印机或错误" value={query} onChange={e=>setQuery(e.target.value)}/></label>
    <label><span>时间范围</span><select aria-label="时间范围" value={timeRange} onChange={e=>setTimeRange(e.target.value)}><option value="all">全部时间</option><option value="today">今天</option><option value="week">近 7 天</option></select></label>
    <label><span>打印机</span><select aria-label="打印机" value={printer} onChange={e=>setPrinter(e.target.value)}><option value="all">全部设备</option>{printers.map(name=><option key={name} value={name}>{name}</option>)}</select></label>
    <label><span>事件类型</span><select aria-label="事件类型" value={eventType} onChange={e=>setEventType(e.target.value)}><option value="all">全部事件</option>{['任务','打印与队列','设备','班级处理','其他'].map(name=><option key={name} value={name}>{name}</option>)}</select></label>
    <label className="runtime-log-problems"><input type="checkbox" checked={onlyProblems} onChange={e=>setOnlyProblems(e.target.checked)}/>只看异常</label>
    <button className="text-button" disabled={loading} onClick={()=>void refresh()}>{loading?'刷新中…':'刷新'}</button>
    <button className="text-button" onClick={resetFilters}>清除筛选</button>
   </div>
  </div>
  {error&&<p role="alert" className="station-notice">{error}</p>}
  <div className="runtime-logs-list" aria-busy={loading}>
   <div className="runtime-log-header"><span>时间</span><span>事件</span><span>打印机</span><span>详细内容</span></div>
   {pageRows.map(log=>{const problem=isProblem(log),message=summary(log);return <article key={log.id} className={'runtime-log'+(problem?' problem':'')}><time dateTime={new Date(log.ts*1000).toISOString()}><span>{localDate(log.ts)}</span><strong>{localTime(log.ts)}</strong></time><div className="runtime-log-event"><strong>{eventName(log.type)}</strong><span className={'runtime-log-state'+(problem?' problem':'')}>{problem?'异常':'正常'}</span></div><div className="runtime-log-printer">{log.printer_name||'—'}</div><div className="runtime-log-content"><strong>{log.title?`任务 #${log.cloud_id} · ${log.title}`:'打印站'}{log.group_label?` · ${log.group_label}`:''}</strong><p>{message}</p>{message!==log.detail&&<details><summary>查看技术详情</summary><pre>{log.type} · {log.detail}</pre></details>}</div></article>})}
   {loading&&!logs.length&&<p className="runtime-log-empty" role="status">正在读取运行日志…</p>}
   {!visible.length&&!loading&&!error&&<p className="runtime-log-empty">没有符合条件的记录</p>}
  </div>
  <div className="runtime-logs-footer"><span>{taskId?`任务 #${taskId}${groupId?` · 班级作业 #${groupId}`:''} · `:''}读取 {logs.length} 条 · 符合 {visible.length} 条</span><div><button disabled={currentPage===1} onClick={()=>setPage(currentPage-1)}>上一页</button><span>{currentPage} / {pages}</span><button disabled={currentPage===pages} onClick={()=>setPage(currentPage+1)}>下一页</button></div></div>
 </section>;
}
