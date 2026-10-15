"use client";
import type {SpecialCopies} from '@/lib/print-model';
import {classCopies} from '@/lib/print-model';

export function ClassCopiesEditor({classes,copies,rules,onChange,disabled=false}: {
 classes:number;copies:number;rules:SpecialCopies[];onChange:(rules:SpecialCopies[])=>void;disabled?:boolean;
}){
 let plan:number[]=[];let error='';
 try{plan=classCopies(classes,copies,rules)}catch(exc){error=exc instanceof Error?exc.message:'逐班份数无效'}
 return <div className="class-copies-editor">
  <div className="class-copies-heading"><strong>特殊班级份数</strong><button type="button" className="text-button" disabled={disabled} onClick={()=>onChange([...rules,{classes:'',copies}])}>添加特殊班级</button></div>
  <p className="hint">填写班级编号，例如 6、10、15 或 1-10；没有填写的班级使用默认份数。</p>
  {rules.map((rule,index)=><div className="special-copies-row" key={index}>
   <label>班级<input aria-label={`特殊班级 ${index+1}`} placeholder="6、10、15" disabled={disabled} value={rule.classes} onChange={e=>onChange(rules.map((item,i)=>i===index?{...item,classes:e.target.value}:item))}/></label>
   <label>每班份数<input aria-label={`特殊份数 ${index+1}`} type="number" min="1" max="10000" disabled={disabled} value={rule.copies} onChange={e=>onChange(rules.map((item,i)=>i===index?{...item,copies:Number(e.target.value)}:item))}/></label>
   <button type="button" className="text-button danger" disabled={disabled} onClick={()=>onChange(rules.filter((_,i)=>i!==index))}>移除</button>
  </div>)}
  {error?<p className="hint" role="alert">{error}</p>:<details className="class-copies-preview"><summary>{classes} 班共 {plan.reduce((sum,value)=>sum+value,0).toLocaleString('zh-CN')} 份 · 查看逐班明细</summary><div>{plan.map((value,index)=><span key={index}>{index+1}班 {value}份</span>)}</div></details>}
 </div>;
}
