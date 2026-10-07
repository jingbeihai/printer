import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from '../prototype/node_modules/typescript/lib/typescript.js';
const source=fs.readFileSync(new URL('../prototype/lib/cloud-api.ts',import.meta.url),'utf8');
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
const input={file:new File(['pdf'],'test.pdf',{type:'application/pdf'}),school:'School',grade:'Grade',classes:2,copies:3,pages:1};
async function scenario(mode){
  const calls=[];
  const context={exports:{},FormData,fetch:async(path,options)=>{
    calls.push({path,options});
    if(path.endsWith('/presign'))return {ok:mode!=='error',json:async()=>mode==='local'?{ok:true,storage_backend:'local'}:mode==='error'?{ok:false,error:'signing failed'}:{ok:true,task_id:7,upload_url:'https://example.invalid/upload'}};
    if(mode==='fallback-error'&&path.endsWith('/file'))return {ok:false,json:async()=>({ok:false,error:'proxy failed'})};
    if(mode==='complete-error'&&path.endsWith('/complete'))return {ok:false,json:async()=>({ok:false,error:'complete failed'})};
    return {ok:true,json:async()=>({ok:true,task_id:7})};
  },XMLHttpRequest:class{upload={};status=200;open(method,url){calls.push({method,url})}setRequestHeader(){}send(){if(mode.startsWith('fallback'))this.onerror();else if(mode==='timeout')this.ontimeout();else this.onload()}}};
  vm.runInNewContext(code,context);
  if(mode==='error'){
    await assert.rejects(()=>context.exports.uploadTask(input),/signing failed/);
    assert.equal(calls.length,1);
  }else if(mode==='fallback-error'||mode==='complete-error'){
    await assert.rejects(()=>context.exports.uploadTask(input),mode==='fallback-error'?/proxy failed/:/complete failed/);
    assert.equal(calls.at(-1).options.method,'DELETE');
    assert.equal(calls.filter(c=>c.path?.endsWith('/file')).length,mode==='fallback-error'?1:0);
  }else{
    await context.exports.uploadTask(input);
    if(mode==='local'){
      assert.equal(calls.length,2);
      assert.ok(calls[1].path.endsWith('/api/v1/tasks'));
      assert.equal(calls[1].options.body.get('copies'),'3');
      assert.equal(calls[1].options.body.get('file').name,'test.pdf');
    }else{
      assert.equal(calls[1].method,'PUT');
      const fallback=mode==='fallback'||mode==='timeout';
      if(fallback){
        assert.ok(calls[2].path.endsWith('/uploads/7/file'));
        assert.equal(calls[2].options.body.get('file').name,'test.pdf');
      }
      assert.ok(calls[fallback?3:2].path.endsWith('/uploads/7/complete'));
      assert.equal(calls.filter(c=>c.path?.endsWith('/presign')).length,1);
    }
  }
}
for(const mode of ['local','cos','error','fallback','timeout','fallback-error','complete-error']){await scenario(mode);console.log(`PASS upload ${mode}`)}
