
(function(){
var L=document.getElementById('login-container'),A=document.getElementById('appContainer');
var B=document.getElementById('loginBtn'),E=document.getElementById('loginError');
var U=document.getElementById('loginUsername'),P=document.getElementById('loginPassword');
var O=document.getElementById('logoutBtn'),M=document.getElementById('messages');
var I=document.getElementById('input'),S=document.getElementById('sendBtn'),T=document.getElementById('stopBtn');
var API='/v2/chat/stream',TK='travel_token',streaming=false,throttle=null,ac=null;
function login(){
fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:U.value,password:P.value})})
.then(function(r){return r.json()})
.then(function(d){localStorage.setItem(TK,d.access_token);E.style.display='none';L.style.display='none';A.classList.add('show');})
.catch(function(e){E.textContent=e.message||'登录失败';E.style.display='block';});
}
function logout(){localStorage.removeItem(TK);L.style.display='flex';A.classList.remove('show');}
async function send(){
if(streaming||!I.value.trim())return;
var text=I.value.trim();I.value='';streaming=true;S.style.display='none';T.style.display='inline-block';
M.innerHTML+='<div class="message" style="align-self:flex-end;background:#10a37f;color:#fff;padding:14px 20px;border-radius:12px;max-width:80%">'+text+'</div>';
M.innerHTML+='<div class="message" style="align-self:flex-start;background:#fff;border:1px solid #ddd;padding:14px 20px;border-radius:12px;max-width:80%" id="LM"><div class="answer-box">思考中...</div></div>';
M.scrollTop=M.scrollHeight;
ac=new AbortController();
T.onclick=function(){ac.abort();done();};
try{
var resp=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+localStorage.getItem(TK)},body:JSON.stringify({query:text}),signal:ac.signal});
if(!resp.ok){if(resp.status===401)logout();else{document.getElementById('LM').querySelector('.answer-box').textContent='请求失败';done();}return;}
var reader=resp.body.getReader(),dec=new TextDecoder('utf-8'),buf='',full='',think='';
while(true){
var r;try{r=await reader.read();}catch(e){if(e.name==='AbortError')break;throw e;}
if(r.done)break;
buf+=dec.decode(r.value,{stream:true});
var lines=buf.split('\n');buf=lines.pop()||'';
for(var i=0;i<lines.length;i++){
var t=lines[i].trim();if(!t.startsWith('data: '))continue;
var d=t.slice(6);if(d==='[DONE]')break;
try{
var j=JSON.parse(d);
if(j.type==='reasoning_chunk')think+=j.content||'';
if(j.type==='answer_chunk'){full=j.content||full;if(!throttle){throttle=setTimeout(function(){throttle=null;var el=document.getElementById('LM');if(el)el.querySelector('.answer-box').textContent=full;},50);}}
if(j.type==='answer_complete'){full=j.content||full;if(throttle){clearTimeout(throttle);throttle=null;}var el=document.getElementById('LM');if(el)el.querySelector('.answer-box').innerHTML=marked.parse(full);}
}catch(e){}
}
}
}catch(e){if(e.name!=='AbortError'){var el=document.getElementById('LM');if(el)el.querySelector('.answer-box').textContent='请求失败';}}
done();
}
function done(){if(throttle){clearTimeout(throttle);throttle=null;}streaming=false;ac=null;S.style.display='inline-block';T.style.display='none';T.onclick=null;}
B.addEventListener('click',login);
P.addEventListener('keydown',function(e){if(e.key==='Enter')login();});
S.addEventListener('click',send);
I.addEventListener('keydown',function(e){if(e.key==='Enter')send();});
O.addEventListener('click',logout);
if(localStorage.getItem(TK)){L.style.display='none';A.classList.add('show');}
})();
