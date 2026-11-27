const STORAGE_KEY='finance-dashboard-offline-vault-v1';
const SNAPSHOT_CACHE='finance-dashboard-encrypted-snapshots-v1';
const SESSION_KEY='finance-dashboard-offline-vault-session-key-v1';
const encoder=new TextEncoder(),decoder=new TextDecoder();
let activeVault;

// Re-selecting an Income Tracker tab returns to its in-page navigation, while
// keeping the dashboard selector available when the user scrolls fully up.
document.addEventListener('click',event=>{
  const link=event.target.closest('a[href]');
  if(!link)return;
  const target=new URL(link.href,location.href);
  if(target.origin===location.origin&&target.pathname===location.pathname&&target.hash==='#top'){
    event.preventDefault();
    history.replaceState(null,'','#top');
    document.getElementById('top')?.scrollIntoView({block:'start',behavior:'smooth'});
  }
});

// Income Tracker keeps ISO dates in storage for safe sorting, but presents them
// in the same month/day/year format as the original Income Manager.
if(/^\/income-(tracker|history|spending)\.html$/.test(location.pathname)){
  const localizeDates=node=>{
    const walker=document.createTreeWalker(node,NodeFilter.SHOW_TEXT);
    const textNodes=[];
    while(walker.nextNode())textNodes.push(walker.currentNode);
    for(const textNode of textNodes){
      const parent=textNode.parentElement;
      if(!parent||parent.closest('script,style'))continue;
      textNode.nodeValue=textNode.nodeValue.replace(/\b(\d{4})-(\d{2})-(\d{2})\b/g,(_,year,month,day)=>`${Number(month)}/${Number(day)}/${year}`);
    }
  };
  localizeDates(document.body);
  new MutationObserver(records=>records.forEach(record=>record.addedNodes.forEach(node=>{
    if(node.nodeType===Node.TEXT_NODE){
      const wrapper=document.createElement('span');
      wrapper.textContent=node.nodeValue;
      localizeDates(wrapper);
      node.nodeValue=wrapper.textContent;
    }else if(node.nodeType===Node.ELEMENT_NODE)localizeDates(node);
  }))).observe(document.body,{childList:true,subtree:true});
}

if(location.pathname.endsWith('/income-tracker.html')){
  const statusNode=document.getElementById('status');
  let statusTimer;
  if(statusNode)new MutationObserver(()=>{
    if(!statusNode.textContent.trim()||statusNode.classList.contains('error'))return;
    clearTimeout(statusTimer);
    statusTimer=setTimeout(()=>{statusNode.textContent='';},3000);
  }).observe(statusNode,{childList:true,characterData:true,subtree:true});
}

const encode=value=>btoa(String.fromCharCode(...new Uint8Array(value)));
const decode=value=>Uint8Array.from(atob(value),char=>char.charCodeAt(0));
const stored=()=>{try{return JSON.parse(localStorage.getItem(STORAGE_KEY)||'null')}catch{return null}};
const persist=value=>localStorage.setItem(STORAGE_KEY,JSON.stringify(value));
const snapshotRequest=path=>new Request('/__offline-vault__/'+encodeURIComponent(path));
async function writeSnapshot(path,value){
  const cache=await caches.open(SNAPSHOT_CACHE);
  await cache.put(snapshotRequest(path),new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}}));
}
async function readSnapshot(path){
  const cache=await caches.open(SNAPSHOT_CACHE),response=await cache.match(snapshotRequest(path));
  return response?response.json():null;
}

async function keyFrom(password,salt){
  const material=await crypto.subtle.importKey('raw',encoder.encode(password),'PBKDF2',false,['deriveKey']);
  return crypto.subtle.deriveKey({name:'PBKDF2',salt,iterations:310000,hash:'SHA-256'},material,{name:'AES-GCM',length:256},true,['encrypt','decrypt']);
}
async function sessionKey(){
  const raw=sessionStorage.getItem(SESSION_KEY);
  return raw?crypto.subtle.importKey('raw',decode(raw),{name:'AES-GCM'},true,['encrypt','decrypt']):null;
}
async function rememberSession(key){
  sessionStorage.setItem(SESSION_KEY,encode(await crypto.subtle.exportKey('raw',key)));
}
function addLockControl(){
  if(document.getElementById('offlineVaultLock'))return;
  const button=document.createElement('button');
  button.id='offlineVaultLock';
  button.className='offline-vault-lock';
  button.type='button';
  button.textContent='Lock offline data';
  button.style.cssText='position:static;z-index:auto;flex:none';
  button.onclick=()=>{sessionStorage.removeItem(SESSION_KEY);location.reload();};
  const personalFinances=document.querySelector('.workspace-tabs a[href="personal-finances.html"]');
  if(personalFinances)personalFinances.insertAdjacentElement('afterend',button);
  else document.body.append(button);
}
async function seal(key,value){
  const iv=crypto.getRandomValues(new Uint8Array(12));
  const bytes=await crypto.subtle.encrypt({name:'AES-GCM',iv},key,encoder.encode(JSON.stringify(value)));
  return {iv:encode(iv),ciphertext:encode(bytes)};
}
async function unseal(key,value){
  const bytes=await crypto.subtle.decrypt({name:'AES-GCM',iv:decode(value.iv)},key,decode(value.ciphertext));
  return JSON.parse(decoder.decode(bytes));
}
function dialog(existing){
  return new Promise((resolve,reject)=>{
    const node=document.createElement('div');
    node.style.cssText='position:fixed;inset:0;z-index:9999;display:grid;place-items:center;padding:20px;background:rgba(15,23,42,.58);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';
    node.innerHTML='<form style="width:min(100%,390px);padding:24px;border-radius:16px;background:#fff;box-shadow:0 24px 60px rgba(0,0,0,.28)"><h1 style="margin:0 0 8px;font-size:22px;color:#172b40">'+(existing?'Unlock dashboard':'Protect offline data')+'</h1><p style="margin:0 0 18px;color:#536e86;font-size:14px;line-height:1.5">'+(existing?'Enter the password that encrypts this phone’s offline financial snapshot.':'Create a password to encrypt financial snapshots saved on this phone. It is never sent to the server and cannot be recovered.')+'</p><label style="display:grid;gap:6px;margin:12px 0;color:#294761;font-size:13px;font-weight:700">Password<input id="vaultPassword" type="password" autocomplete="'+(existing?'current-password':'new-password')+'" required minlength="12" style="padding:11px;border:1px solid #cdd9e5;border-radius:8px;font:inherit"></label>'+(existing?'':'<label style="display:grid;gap:6px;margin:12px 0;color:#294761;font-size:13px;font-weight:700">Confirm password<input id="vaultConfirm" type="password" autocomplete="new-password" required minlength="12" style="padding:11px;border:1px solid #cdd9e5;border-radius:8px;font:inherit"></label>')+'<p id="vaultError" role="alert" style="min-height:18px;margin:10px 0;color:#c34350;font-size:13px"></p><button type="submit" style="width:100%;padding:11px;border:0;border-radius:8px;background:#1e5a94;color:#fff;font:inherit;font-weight:700">'+(existing?'Unlock':'Create password')+'</button>'+(existing?'<button id="vaultReset" type="button" style="width:100%;margin-top:10px;padding:8px;border:0;background:transparent;color:#64788d;font:inherit;font-size:12px;text-decoration:underline">Forget password and erase this phone’s offline snapshot</button>':'')+'</form>';
    document.body.append(node);
    const form=node.querySelector('form'),error=node.querySelector('#vaultError'),password=node.querySelector('#vaultPassword');
    password.focus();
    form.onsubmit=event=>{event.preventDefault();const confirm=node.querySelector('#vaultConfirm');if(confirm&&password.value!==confirm.value){error.textContent='Passwords do not match.';return;}resolve({password:password.value,node});};
    node.querySelector('#vaultReset')?.addEventListener('click',()=>{if(confirm('Erase this phone’s encrypted offline dashboard snapshot? You will need to reconnect to sync it again.')){localStorage.removeItem(STORAGE_KEY);sessionStorage.removeItem(SESSION_KEY);resolve({reset:true,node});}});
  });
}
async function unlock(forceSetup=false){
  if(!window.crypto?.subtle)throw Error('This browser cannot securely encrypt offline data.');
  let record=forceSetup?null:stored();
  if(record){
    try{
      const remembered=await sessionKey();
      if(remembered){await unseal(remembered,record.verifier);return {key:remembered,record};}
    }catch{sessionStorage.removeItem(SESSION_KEY);}
  }
  let answer=await dialog(Boolean(record));
  if(answer.reset){answer.node.remove();return unlock(true);}
  const salt=record?decode(record.salt):crypto.getRandomValues(new Uint8Array(16)),key=await keyFrom(answer.password,salt);
  try{
    if(record)await unseal(key,record.verifier);
    else{record={version:1,salt:encode(salt),records:{},verifier:await seal(key,{verified:true})};persist(record);}
    await rememberSession(key);
    answer.node.remove();
    return {key,record};
  }catch{
    answer.node.remove();
    return unlock(forceSetup);
  }
}
export async function getVault(forceSetup=false){
  if(!activeVault||forceSetup){
    activeVault=(async()=>{const {key,record}=await unlock(forceSetup);addLockControl();navigator.storage?.persist?.().catch(()=>{});const storeJSON=async(path,value)=>{const encrypted=await seal(key,value);record.records[path]=encrypted;persist(record);await writeSnapshot(path,encrypted);};return {
      storeJSON,
      async getJSON(path){
        try{
          const response=await fetch(path);
          if(!response.ok)throw Error('Unable to load the current dashboard data.');
          const value=await response.json();
          await storeJSON(path,value);
          return {value,offline:false};
        }catch(error){
          const snapshot=await readSnapshot(path)||record.records[path];
          if(!snapshot)throw error;
          return {value:await unseal(key,snapshot),offline:true};
        }
      }
    };})();
  }
  return activeVault;
}
