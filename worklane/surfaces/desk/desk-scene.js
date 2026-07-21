<script>
"use strict";
/* Suite dark/light — shared with Office / Roster (protocolcity-theme). */
(function(){
  var KEY='protocolcity-theme';
  function theme(){
    try{ var t=localStorage.getItem(KEY)||localStorage.getItem('wl-theme')||'light';
      return t==='dark'?'dark':'light'; }
    catch(e){ return 'light'; }
  }
  function apply(t){
    t=(t==='dark')?'dark':'light';
    document.documentElement.setAttribute('data-theme', t);
    try{ localStorage.setItem(KEY,t); localStorage.setItem('wl-theme',t); }catch(e){}
    var btn=document.getElementById('theme-toggle');
    if(btn){
      btn.textContent = t==='dark' ? '\\u2600' : '\\u263D';
      btn.title = t==='dark' ? 'Switch to light theme' : 'Switch to dark theme';
      btn.setAttribute('aria-label', btn.title);
    }
  }
  function toggle(){ apply(theme()==='dark'?'light':'dark'); }
  apply(theme());
  var btn=document.getElementById('theme-toggle');
  if(btn) btn.addEventListener('click', toggle);
})();
var SCENE=null, seenFiled={}, firstPoll=true;
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function $(id){return document.getElementById(id);}
function ago(iso){if(!iso)return "";var t=Date.parse(iso);if(isNaN(t))return "";
  var s=Math.max(0,(Date.now()-t)/1000|0);
  if(s>=86400)return (s/86400|0)+"d ago"; if(s>=3600)return (s/3600|0)+"h ago";
  if(s>=60)return (s/60|0)+"m ago"; return s+"s ago";}
function sheets(n,cap){var out="",m=Math.min(n,cap||12);
  for(var i=0;i<m;i++){var rot=((i*37)%7)-3;
    out+='<div class="sheet" style="bottom:'+(i*4)+'px;transform:rotate('+(rot/3)+'deg)"></div>';}
  return out;}
function pile(cls,label,n){
  return '<div class="pile '+cls+'"><div class="sheets">'+sheets(n)+'</div>'+
    '<div class="n">'+esc(n)+'</div><div class="l">'+esc(label)+'</div></div>';}
/* wl-168: paper-line stations — status → station id for counts & flyers */
var PL_STATUS={backlog:"plFiled",in_progress:"plClaimed",in_review:"plSignoff",done:"plSigned"};
var PL_SEEN={}, PL_ACTIVE=0, PL_MAX=6, PL_MS=1500, firstPlPoll=true;
function plCounts(d){
  var c={filed:0,claimed:0,signoff:0,signed:0};
  (d.stores||[]).forEach(function(s){
    if(TRAY_F!=="all"&&s.slug!==TRAY_F)return;
    c.filed+=s.backlog||0; c.claimed+=s.in_progress||0; c.signoff+=s.in_review||0;});
  (d.filed||[]).forEach(function(f){
    if(TRAY_F!=="all"&&f.store!==TRAY_F)return; c.signed++;});
  return c;}
function renderPaperLine(d){
  var c=plCounts(d);
  var n=$("plFiledN"); if(n)n.textContent=c.filed;
  n=$("plClaimedN"); if(n)n.textContent=c.claimed;
  n=$("plSignoffN"); if(n)n.textContent=c.signoff;
  n=$("plSignedN"); if(n)n.textContent=c.signed;
  paintStationFocus();
  /* first poll seeds seen-set so we only animate transitions after load.
     Deep-link / cabinet skim: replay up to 3 recent flyers for this scope
     so the paper-line still feels alive when Office lands you filtered. */
  var trs=d.recent_transitions||[];
  if(firstPlPoll){
    var replay=[];
    trs.forEach(function(t){
      if(!t||!t.id)return;
      PL_SEEN[t.id]=1;
      if(TRAY_F!=="all"&&t.store&&t.store!==TRAY_F)return;
      if(STATUS_F&&t.to_status&&t.to_status!==STATUS_F&&t.from_status!==STATUS_F)return;
      replay.push(t);
    });
    firstPlPoll=false;
    if(replay.length){
      setTimeout(function(){
        replay.slice(0,3).reverse().forEach(function(t,i){
          setTimeout(function(){ flyPaper(t); }, i*220);
        });
      }, 380);
    }
    return;
  }
  trs.slice().reverse().forEach(function(t){ /* oldest first so flyers chain L→R */
    if(!t||!t.id||PL_SEEN[t.id])return;
    PL_SEEN[t.id]=1;
    if(TRAY_F!=="all"&&t.store&&t.store!==TRAY_F)return;
    flyPaper(t);});}
function stationCenter(status){
  var id=PL_STATUS[status]; if(!id)return null;
  var el=$(id); if(!el)return null;
  var obj=el.querySelector(".pl-obj")||el;
  var r=obj.getBoundingClientRect();
  return {x:r.left+r.width/2, y:r.top+r.height/2};}
function flyPaper(tr){
  if(PL_ACTIVE>=PL_MAX)return;
  var b=stationCenter(tr.to_status);
  if(!b)return;
  var a=stationCenter(tr.from_status);
  /* Birth filings (created → backlog): no from-station — enter from the
     left of the paper line so the sheet still lands on Filed. */
  if(!a){
    var line=$("paperLine");
    if(!line)return;
    var lr=line.getBoundingClientRect();
    a={x:lr.left+16, y:lr.top+lr.height/2};
  }
  if(Math.abs(a.x-b.x)<4&&Math.abs(a.y-b.y)<4)return;
  PL_ACTIVE++;
  var el=document.createElement("div");
  el.className="pl-flyer";
  el.innerHTML='<div class="sheet"></div>'+spriteChip(tr.author||"");
  el.style.left=a.x+"px"; el.style.top=a.y+"px"; el.style.opacity="1";
  document.body.appendChild(el);
  var t0=Date.now();
  var iv=setInterval(function(){
    var p=Math.min(1,(Date.now()-t0)/PL_MS);
    var e=p*p*(3-2*p); /* smoothstep */
    var x=a.x+(b.x-a.x)*e, y=a.y+(b.y-a.y)*e - Math.sin(p*Math.PI)*18;
    el.style.left=x+"px"; el.style.top=y+"px";
    if(p>=0.85)el.style.opacity=String(Math.max(0,(1-p)/0.15));
    if(p>=1){clearInterval(iv); if(el.parentNode)el.parentNode.removeChild(el); PL_ACTIVE--;}
  },32);}
function stampFor(kind){
  /* Attention stamps + station-skim stamps (wl-186 film from Office counts). */
  if(kind==="in_review"||kind==="signoff")return {txt:"SIGN-OFF DUE",cls:""};
  if(kind==="founder_decision")return {txt:"AWAITING SIGNATURE",cls:""};
  if(kind==="human_gate")return {txt:"AT THE WINDOW",cls:"amber"};
  if(kind==="embargo"||kind==="timer")return {txt:"EMBARGO",cls:"amber"};
  if(kind==="filed"||kind==="ready"||kind==="backlog")return {txt:"FILED",cls:"amber"};
  if(kind==="claimed"||kind==="in_flight"||kind==="in_progress")return {txt:"CLAIMED",cls:""};
  if(kind==="signed"||kind==="done")return {txt:"SIGNED",cls:"green"};
  return {txt:"GONE QUIET",cls:""};}
function formHtml(it,slip){
  /* Title-first slips: id · priority · stamp · title (+ sitting age).
     Long reason text belongs in the drawer, not on the tray.
     Whole slip opens the work-order drawer (wl-145); id link remains
     the cmd-click escape hatch to the full record. */
  var st=stampFor(it.kind);
  var sit=it.waiting_since?'<div class="meta sit">sitting '+esc(ago(it.waiting_since))+'</div>':'';
  var href=it.url||("/admin/tasks/"+it.id);
  return '<div class="form'+(slip?' slip':'')+'" data-id="'+esc(it.id)+'" title="Open work order">'+
    '<div class="stamp '+st.cls+'">'+esc(st.txt)+'</div>'+
    '<div class="no"><a href="'+esc(href)+'">'+
      esc(it.id)+'</a> \\u00b7 P'+esc(it.priority)+
      ' \\u00b7 <span class="tag">'+esc(it.product)+'</span></div>'+
    '<div class="t">'+esc(it.title)+'</div>'+sit+'</div>';}
/* City DNA (pc-40 / CITY_DNA.md): identity registry shared with the plat
   and dispatch — same hash, same palette, same little person everywhere.
   The local founder identity and non-roster authors wear the gold founder chip. */
var DNA_PALETTE=["#3d7a6a","#a8842c","#4a6fa5","#7d5185","#a35b3a","#5f7d3a"];
function dnaHash(s){var h=0,i;for(i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))|0;return Math.abs(h);}
function spriteChip(author){
  var a=String(author||""); if(!a)return "";
  var roster=/^claude-/.test(a)||/^(grok|codex|cursor)$/.test(a);
  var col=roster?DNA_PALETTE[dnaHash(a)%DNA_PALETTE.length]:"#e9c46a";
  return '<svg class="citizen" viewBox="-5 -23 10 24" aria-hidden="true"><title>'+esc(a)+'</title>'+
    '<line x1="-2" y1="-4" x2="-2" y2="0" stroke="#4a3f2c" stroke-width="2"/>'+
    '<line x1="2" y1="-4" x2="2" y2="0" stroke="#4a3f2c" stroke-width="2"/>'+
    '<rect x="-4" y="-14" width="8" height="11" rx="2.5" fill="'+col+'"/>'+
    '<circle cx="0" cy="-18" r="4.2" fill="#d9b98c" stroke="#4a3f2c" stroke-width=".7"/></svg>';}
/* wl-186 / suite Click ladder: cabinet + status skim live in the URL so
   Office Work-rail counts can land on the filtered film. */
var TRAY_F="all", STATUS_F="";
var STATUS_LABEL={backlog:"filed",in_progress:"claimed",in_review:"sign-off",done:"signed"};
var STATUS_SKIM=null, STATUS_SKIM_KEY="";
function deskQuery(){
  try{ return new URLSearchParams(location.search||""); }catch(err){ return new URLSearchParams(); }
}
function normalizeCabinet(raw, known){
  var s=String(raw||"").trim(); if(!s||s==="all") return "all";
  if(known&&known[s]) return s;
  var low=s.toLowerCase();
  if(known){
    if(known[low]) return low;
    for(var k in known){ if(k.toLowerCase()===low) return k; }
  }
  return low;
}
function syncDeskQuery(){
  try{
    var u=new URL(location.href);
    if(TRAY_F&&TRAY_F!=="all") u.searchParams.set("cabinet",TRAY_F);
    else u.searchParams.delete("cabinet");
    if(STATUS_F) u.searchParams.set("status",STATUS_F);
    else u.searchParams.delete("status");
    /* keep ?open= while drawer is up — closeWO clears it */
    var qs=u.searchParams.toString();
    history.replaceState({},"",u.pathname+(qs?("?"+qs):"")+u.hash);
  }catch(err){}
}
function bootFiltersFromQuery(){
  var q=deskQuery();
  var cab=q.get("cabinet")||q.get("store")||"";
  var st=q.get("status")||"";
  if(cab) TRAY_F=normalizeCabinet(cab, null);
  else TRAY_F=localStorage.getItem("wl_desk_tray_filter")||"all";
  if(st&&STATUS_LABEL[st]) STATUS_F=st;
  else STATUS_F="";
}
bootFiltersFromQuery();
function storeDisplay(slug){
  var stores=(SCENE&&SCENE.stores)||[];
  for(var i=0;i<stores.length;i++){
    if(stores[i].slug===slug) return stores[i].display||slug;
  }
  return slug;}
function pileHtml(cls,label,n,status){
  var on=STATUS_F&&STATUS_F===status;
  var dim=STATUS_F&&STATUS_F!==status;
  return '<div class="pile '+cls+(on?' on':'')+(dim?' dim':'')+
    '" data-status="'+esc(status)+'" title="Skim '+esc(label)+' on this desk">'+
    '<div class="sheets">'+sheets(n)+'</div>'+
    '<div class="n">'+esc(n)+'</div><div class="l">'+esc(label)+'</div></div>';}
function paintStationFocus(){
  document.querySelectorAll(".pl-station").forEach(function(btn){
    var st=btn.getAttribute("data-status")||"";
    btn.classList.toggle("on", !!(STATUS_F&&STATUS_F===st));
  });}
function statusSkimUrl(){
  var q="status="+encodeURIComponent(STATUS_F)+"&limit=48";
  if(TRAY_F&&TRAY_F!=="all") q+="&product="+encodeURIComponent(TRAY_F);
  return "/api/admin/tasks?"+q;
}
function statusSkimKind(){
  if(STATUS_F==="in_review") return "signoff";
  if(STATUS_F==="in_progress") return "claimed";
  if(STATUS_F==="done") return "signed";
  if(STATUS_F==="backlog") return "filed";
  return "filed";
}
function taskToForm(t){
  return {id:t.id, title:t.title, priority:t.priority,
    product:TRAY_F!=="all"?TRAY_F:(t.product||t.store||""),
    kind:statusSkimKind(),
    waiting_since:t.updated_at||t.created_at||t.closed_at||"",
    url:"/admin/tasks/"+t.id};
}
function filedToForm(f){
  return {id:f.id, title:f.title, priority:f.priority||3,
    product:f.store||TRAY_F||"", kind:"signed",
    waiting_since:f.closed_at||"", url:"/admin/tasks/"+f.id};
}
function renderStatusSkim(){
  if(!STATUS_F){
    STATUS_SKIM=null; STATUS_SKIM_KEY=""; return false;
  }
  var key=TRAY_F+"|"+STATUS_F;
  /* Signed station: film the outbox receipts in the tray (same carbon copies). */
  if(STATUS_F==="done"){
    var d=SCENE||{};
    var signed=[];
    (d.filed||[]).forEach(function(f){
      if(TRAY_F!=="all"&&f.store!==TRAY_F&&String(f.store||"").toLowerCase()!==TRAY_F)return;
      signed.push(filedToForm(f));
    });
    STATUS_SKIM=signed; STATUS_SKIM_KEY=key;
    $("decisionsStack").innerHTML = signed.length
      ? signed.map(function(it){return formHtml(it,false);}).join("")
      : '<div class="empty-note">No signed receipts'+(TRAY_F!=="all"?(" in "+esc(storeDisplay(TRAY_F))):"")+
        ' in the window</div>';
    $("staleStack").innerHTML='<div class="empty-note">Status skim \\u2014 hold bin paused</div>';
    $("inCount").textContent=signed.length; $("holdCount").textContent=0;
    return true;
  }
  if(STATUS_SKIM&&STATUS_SKIM_KEY===key){
    var rows=STATUS_SKIM;
    $("decisionsStack").innerHTML = rows.length
      ? rows.map(function(it){return formHtml(it,false);}).join("")
      : '<div class="empty-note">No '+esc(STATUS_LABEL[STATUS_F]||STATUS_F)+
        ' tickets'+(TRAY_F!=="all"?(" in "+esc(storeDisplay(TRAY_F))):"")+'</div>';
    $("staleStack").innerHTML='<div class="empty-note">Status skim \\u2014 hold bin paused</div>';
    $("inCount").textContent=rows.length; $("holdCount").textContent=0;
    return true;
  }
  $("decisionsStack").innerHTML='<div class="empty-note">Pulling '+esc(STATUS_LABEL[STATUS_F]||STATUS_F)+'\\u2026</div>';
  $("staleStack").innerHTML='<div class="empty-note">Status skim \\u2014 hold bin paused</div>';
  $("inCount").textContent="\\u2026"; $("holdCount").textContent=0;
  var fetchKey=key;
  fetch(statusSkimUrl(),{cache:"no-store"}).then(function(r){return r.json();}).then(function(d){
    if(fetchKey!==(TRAY_F+"|"+STATUS_F)) return;
    var tasks=(d&&d.tasks)||[];
    STATUS_SKIM=tasks.map(taskToForm);
    STATUS_SKIM_KEY=fetchKey;
    render();
  }).catch(function(){
    if(fetchKey!==(TRAY_F+"|"+STATUS_F)) return;
    STATUS_SKIM=[]; STATUS_SKIM_KEY=fetchKey; render();
  });
  return true;
}
function render(){
  var d=SCENE; if(!d)return;
  var att=d.attention||[];
  /* Cabinet skim — ledgers are the nav; validate remembered pick (case-insensitive). */
  var known={}; (d.stores||[]).forEach(function(s){ known[s.slug]=1; });
  if(TRAY_F!=="all"){
    TRAY_F=normalizeCabinet(TRAY_F, known);
    if(!known[TRAY_F]) TRAY_F="all";
  }
  paintStationFocus();
  /* Tray headers track the film — station skim vs default needs-you. */
  var inHead=$("inTrayTitle");
  if(inHead){
    if(STATUS_F){
      inHead.innerHTML='Skim \\u00b7 '+esc(STATUS_LABEL[STATUS_F]||STATUS_F)+
        (TRAY_F!=="all"?(' \\u00b7 '+esc(storeDisplay(TRAY_F))):'')+
        ' (<span id="inCount">0</span>)';
    } else {
      inHead.innerHTML='In-tray \\u00b7 needs you (<span id="inCount">0</span>)';
    }
  }
  var skimmed=renderStatusSkim();
  if(!skimmed){
    var inTray=[], hold=[];
    att.forEach(function(it){
      if(TRAY_F!=="all"&&it.product!==TRAY_F&&String(it.product||"").toLowerCase()!==TRAY_F)return;
      (it.kind==="stalled"?hold:inTray).push(it); });
    $("decisionsStack").innerHTML = inTray.length
      ? inTray.map(function(it){return formHtml(it,false);}).join("")
      : '<div class="empty-note">Tray empty \\u2014 nothing to sign</div>';
    $("staleStack").innerHTML = hold.length
      ? hold.map(function(it){return formHtml(it,true);}).join("")
      : '<div class="empty-note">Nothing gone quiet</div>';
    $("inCount").textContent=inTray.length; $("holdCount").textContent=hold.length;
  }

  /* Blotter chrome: show-all lives here when a cabinet or status is scoped */
  var scopeEl=$("blotterScope");
  if(scopeEl){
    if(TRAY_F==="all"&&!STATUS_F){
      scopeEl.innerHTML='<p class="blotter-hint">Click a cabinet or station to skim \\u00b7 board stays on the ledger</p>';
    } else {
      var bits=[];
      if(TRAY_F!=="all") bits.push('<strong>'+esc(storeDisplay(TRAY_F))+'</strong>');
      if(STATUS_F) bits.push(esc(STATUS_LABEL[STATUS_F]||STATUS_F));
      scopeEl.innerHTML='<span class="skim-chip" title="Desk skim from Office / stations">'+
        'Skimming '+bits.join(" \\u00b7 ")+
        '<button type="button" id="clearSkim" data-slug="all">Show all</button></span>';
    }
  }

  var openAll=0, readyAll=0, doneAll=0;
  (d.stores||[]).forEach(function(s){
    openAll+=s.backlog+s.in_progress+s.in_review;
    readyAll+=s.ready||0; doneAll+=s.done_total||0;});
  var readyOn=STATUS_F==="backlog"&&TRAY_F==="all";
  var hz='<div class="hood all-hood'+(TRAY_F==="all"?' on':'')+'" data-slug="all">'+
    '<div class="hood-head"><span class="nm"><button type="button" class="hood-scope" data-slug="all"'+
    ' title="Show every cabinet">All cabinets</button></span>'+
    '<span class="ready-chip'+(readyAll?'':' zero')+(readyOn?' on':'')+
    '" data-ready-slug="all" title="Skim ready (filed) across cabinets">'+esc(readyAll)+' ready</span></div>'+
    '<div class="all-sub">'+esc(att.length)+' needing you \\u00b7 '+esc(openAll)+
    ' open across the city \\u00b7 click again on a scoped cabinet also clears</div></div>';
  (d.stores||[]).forEach(function(s){
    var open=s.backlog+s.in_progress+s.in_review;
    var on=TRAY_F===s.slug, dim=TRAY_F!=="all"&&!on;
    var rOn=STATUS_F==="backlog"&&TRAY_F===s.slug;
    hz+='<div class="hood'+(on?' on':'')+(dim?' dim':'')+'" data-slug="'+esc(s.slug)+'">'+
      '<div class="hood-head">'+
      '<span class="nm"><button type="button" class="hood-scope" data-slug="'+esc(s.slug)+'"'+
      ' title="Skim Desk to this cabinet">'+esc(s.display||s.slug)+'</button>'+
      ' <span class="tag">'+esc(s.prefix)+'-</span>'+
      ' <a class="hood-board" href="/admin/tickets/'+esc(s.slug)+'" title="Open Board for this cabinet">board</a></span>'+
      '<span class="ready-chip'+(s.ready?'':' zero')+(rOn?' on':'')+
      '" data-ready-slug="'+esc(s.slug)+'" title="Skim ready (filed) for this cabinet">'+
      esc(s.ready)+' ready</span></div>'+
      '<div class="piles">'+pileHtml("backlog","filed",s.backlog,"backlog")+
      pileHtml("doing","claimed",s.in_progress,"in_progress")+
      pileHtml("review","sign-off",s.in_review,"in_review")+
      '</div>'+
      '<div class="empty-note" style="margin-top:6px">'+esc(open)+' open work orders \\u00b7 '+
      esc(s.done_total)+' signed off, ever</div></div>';});
  $("hoodList").innerHTML = hz || '<div class="empty-note">No ledgers yet \\u2014 no stores discovered</div>';

  /* Outbox + stamp pad always live — cabinet skim narrows them; status skim
     only filters the left trays (Office deep-link film must not kill the room). */
  var filed=d.filed||[], freshCount=0, cz="", shown=0;
  filed.forEach(function(f){
    if(TRAY_F!=="all"&&f.store!==TRAY_F&&String(f.store||"").toLowerCase()!==TRAY_F)return;
    shown++;
    var fresh=!firstPoll && !seenFiled[f.id]; if(fresh)freshCount++;
    var hi=STATUS_F==="done"?" on-skim":"";
    cz+='<div class="clip-item'+(fresh?' fresh':'')+hi+'">'+spriteChip(f.author)+
      '<span class="when">'+esc(ago(f.closed_at))+'</span> '+
      '<a href="/admin/tasks/'+esc(f.id)+'">'+esc(f.id)+'</a> '+
      '<span class="dim">['+esc(f.store)+']</span> '+esc(String(f.title).slice(0,64))+'</div>';});
  $("shippedClip").innerHTML = cz || '<div class="clip-item empty-note">No carbon copies in the window</div>';
  filed.forEach(function(f){seenFiled[f.id]=1;});
  $("padCount").textContent=shown;
  $("padWindow").textContent=(TRAY_F!=="all"?"signed \\u00b7 "+storeDisplay(TRAY_F)+" \\u00b7 ":"signed \\u00b7 ")+
    "last "+(d.window_hours||24)+"h";
  if(freshCount>0)thunk();
  renderPaperLine(d);
  try{ fwFillStores(d); tnPickReady(d); }catch(e){}
  firstPoll=false;}
function thunk(){
  var r=$("rubberStamp"), ink=$("inkRing"); if(!r)return;
  r.classList.remove("thunk"); ink.classList.remove("show");
  void r.getBoundingClientRect();
  r.classList.add("thunk"); ink.classList.add("show");}
function poll(){
  fetch("/api/scene",{cache:"no-store"}).then(function(r){
    if(!r.ok)throw 0; return r.json();
  }).then(function(d){SCENE=d; render();
    $("liveChip").className=""; $("liveChip").textContent="LIVE";
  }).catch(function(){
    $("liveChip").className="hold"; $("liveChip").textContent=SCENE?"HOLDING":"NO SIGNAL";});}
poll(); setInterval(poll,15000);
/* wl-179: wall clock only — plat sky/sun band retired with the cabinet pivot. */
setInterval(function(){
  var n=new Date();
  $("clock").textContent=n.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",second:"2-digit"});
},1000);
(function(){ var n=new Date();
  $("clock").textContent=n.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",second:"2-digit"}); })();

/* pc-163: Desk D0 search — same chrome slot as Office; scope = tickets */
var SR_IX=-1, SR_HITS=[];
function deskSearchCorpus(){
  var out=[], seen={}, d=SCENE||{};
  function add(id,title,meta,kind){
    if(!id||seen[id])return; seen[id]=1;
    out.push({id:id, title:title||"", meta:meta||"", kind:kind||"ticket"});
  }
  (d.attention||[]).forEach(function(it){
    add(it.id, it.title, (it.product||"")+" \\u00b7 "+(it.kind||"attention"), "attention");
  });
  (d.filed||[]).forEach(function(f){
    add(f.id, f.title, (f.store||"")+" \\u00b7 signed", "filed");
  });
  (d.stores||[]).forEach(function(s){
    /* store names are findable; pick jumps to that ledger */
    if(!s.slug)return;
    out.push({id:"store:"+s.slug, title:s.display||s.slug, meta:(s.prefix||"")+"- \\u00b7 ledger",
      kind:"store", href:"/admin/desk?cabinet="+encodeURIComponent(s.slug)});
  });
  return out;
}
function runDeskSearch(q, keepIx){
  q=String(q||"").trim().toLowerCase();
  var box=$("searchResults"), field=$("searchField");
  if(!box||!field)return;
  if(!q){ box.classList.add("tucked"); field.setAttribute("aria-expanded","false");
    SR_IX=-1; SR_HITS=[]; return; }
  var hits=deskSearchCorpus().filter(function(h){
    return (h.id+" "+h.title+" "+h.meta).toLowerCase().indexOf(q)>=0;
  }).slice(0,12);
  /* Exact-ish id: also offer open even if not in scene window */
  if(/^[a-z]{1,6}-\\d+$/i.test(q) && !hits.some(function(h){return String(h.id).toLowerCase()===q;})){
    hits.unshift({id:q, title:"open "+q, meta:"lookup by id", kind:"id"});
  }
  SR_HITS=hits;
  if(keepIx){ SR_IX=Math.max(0, Math.min(hits.length-1, SR_IX)); }
  else { SR_IX=hits.length?0:-1; }
  if(!hits.length){
    box.innerHTML='<div class="sr-empty">No tickets match \\u2014 try an id like pc-163</div>';
  } else {
    var html="", last="";
    hits.forEach(function(h,i){
      var g=h.kind==="store"?"ledgers":(h.kind==="filed"?"signed":"work orders");
      if(g!==last){ html+='<div class="sr-group">'+g+'</div>'; last=g; }
      html+='<button type="button" class="sr-item'+(i===SR_IX?' is-active':'')+
        '" data-ix="'+i+'" role="option">'+
        '<span class="sr-chip">'+esc(String(h.id).replace(/^store:/,"").slice(0,10))+'</span>'+
        '<span class="sr-body"><div class="sr-title">'+esc(h.title||h.id)+'</div>'+
        '<div class="sr-meta">'+esc(h.meta)+'</div></span></button>';
    });
    box.innerHTML=html;
  }
  box.classList.remove("tucked");
  field.setAttribute("aria-expanded","true");
}
function pickDeskSearch(ix){
  var h=SR_HITS[ix]; if(!h)return;
  $("searchResults").classList.add("tucked");
  $("searchField").setAttribute("aria-expanded","false");
  $("searchField").blur();
  if(h.href){ location.href=h.href; return; }
  if(h.kind==="store"){ location.href="/admin/desk?cabinet="+encodeURIComponent(String(h.id).replace(/^store:/,"")); return; }
  openWO(h.id);
}
(function wireDeskSearch(){
  var field=$("searchField"), box=$("searchResults");
  if(!field||!box)return;
  field.addEventListener("input", function(){ runDeskSearch(field.value, false); });
  field.addEventListener("keydown", function(e){
    if(e.key==="ArrowDown"){ e.preventDefault();
      if(box.classList.contains("tucked")) runDeskSearch(field.value, false);
      if(!SR_HITS.length)return;
      SR_IX=Math.min(SR_HITS.length-1, SR_IX+1); runDeskSearch(field.value, true); }
    else if(e.key==="ArrowUp"){ e.preventDefault(); if(!SR_HITS.length)return;
      SR_IX=Math.max(0, SR_IX-1); runDeskSearch(field.value, true); }
    else if(e.key==="Enter"){ e.preventDefault();
      if(box.classList.contains("tucked")||SR_IX<0) runDeskSearch(field.value, false);
      if(SR_IX>=0) pickDeskSearch(SR_IX); }
    else if(e.key==="Escape"){ box.classList.add("tucked"); field.setAttribute("aria-expanded","false"); }
  });
  box.addEventListener("mousedown", function(e){
    var btn=e.target.closest(".sr-item"); if(!btn)return;
    e.preventDefault(); pickDeskSearch(+btn.getAttribute("data-ix"));
  });
  document.addEventListener("click", function(e){
    if(!e.target.closest("#searchlight")) box.classList.add("tucked");
  });
})();

/* ── the work-order drawer (wl-145): tickets open ON the desk ── */
var WO_ID=null, WO_TASK=null;
var FD_LABELS={"needs:founder-decision":1,"founder-decision":1};
function founderAuthor(){return (window.FOUNDER&&FOUNDER.founder_id)||"founder";}

/* ── wl-154: front window intake + take-a-number claim ── */
var TN_READY=null;
function fwAuthor(){
  return (window.FOUNDER&&(FOUNDER.founder_id||FOUNDER.author||FOUNDER.identity))
    ||"you";
}
function fwFillStores(d){
  var sel=$("fwStore"); if(!sel)return;
  var cur=sel.value;
  var opts=(d.stores||[]).slice().sort(function(a,b){
    return String(a.display||a.slug).localeCompare(String(b.display||b.slug));
  });
  if(!opts.length){
    sel.innerHTML='<option value="">(no stores)</option>';
    return;
  }
  sel.innerHTML=opts.map(function(s){
    return '<option value="'+esc(s.slug)+'">'+esc(s.display||s.slug)+
      ' ('+esc(s.prefix||'?')+')</option>';
  }).join("");
  if(cur){ for(var i=0;i<sel.options.length;i++){
    if(sel.options[i].value===cur){ sel.selectedIndex=i; break; }
  }}
  /* Prefer tray filter cabinet when set */
  if(typeof TRAY_F==="string"&&TRAY_F&&TRAY_F!=="all"){
    for(var j=0;j<sel.options.length;j++){
      if(sel.options[j].value===TRAY_F){ sel.selectedIndex=j; break; }
    }
  }
}
function fwSubmit(ev){
  if(ev)ev.preventDefault();
  var st=$("fwStatus"), btn=$("fwSubmitBtn");
  var project=($("fwStore")&&$("fwStore").value)||"";
  var title=($("fwTitle")&&$("fwTitle").value||"").trim();
  var desc=($("fwBody")&&$("fwBody").value||"").trim();
  var prio=parseInt(($("fwPriority")&&$("fwPriority").value)||"3",10);
  var labRaw=($("fwLabels")&&$("fwLabels").value||"").trim();
  var labels=labRaw?labRaw.split(/[,\\s]+/).map(function(s){return s.trim();}).filter(Boolean):[];
  if(!project||!title||!desc){
    if(st){ st.className="fw-status err"; st.textContent="store, title, and description required"; }
    return false;
  }
  if(btn)btn.disabled=true;
  if(st){ st.className="fw-status"; st.textContent="filing…"; }
  fetch("/api/admin/tasks",{
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({
      project:project, product:project,
      title:title, description:desc, priority:prio,
      labels:labels, author:fwAuthor()
    })
  }).then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});})
   .then(function(x){
     if(!x.ok||!(x.d&&x.d.ok)){
       if(st){ st.className="fw-status err";
         st.textContent=(x.d&&(x.d.error||x.d.detail))||"file failed"; }
       return;
     }
     var id=(x.d.task&&(x.d.task.id||x.d.task.ext_id))||x.d.id||"ok";
     if(st){ st.className="fw-status ok"; st.textContent="filed "+id;
       setTimeout(function(){ if(st&&st.textContent==="filed "+id){ st.className="fw-status"; st.textContent=""; } }, 5000);
     }
     if($("fwTitle"))$("fwTitle").value="";
     if($("fwBody"))$("fwBody").value="";
     if($("fwLabels"))$("fwLabels").value="";
     try{ poll(); }catch(e){}
     try{ if(typeof openWO==="function") openWO(id); }catch(e){}
   }).catch(function(){
     if(st){ st.className="fw-status err"; st.textContent="network error — try again"; }
   }).then(function(){ if(btn)btn.disabled=false; });
  return false;
}
function tnPickReady(d){
  var stores=(d.stores||[]).slice();
  var prefer=typeof TRAY_F==="string"&&TRAY_F&&TRAY_F!=="all"?TRAY_F:"";
  var slip=$("tnSlip"), btn=$("tnClaimBtn");
  var target=null;
  if(prefer){
    /* Cabinet scoped: only show ready from that store — no fallthrough to other stores */
    for(var i=0;i<stores.length;i++){
      if(stores[i].slug===prefer){ target=stores[i]; break; }
    }
    if(!target||(target.ready||0)===0){
      TN_READY=null;
      var disp=target?(target.display||target.slug):prefer;
      if(slip)slip.innerHTML='<span class="empty-note">No ready work in '+esc(disp)+'</span>';
      if(btn)btn.disabled=true;
      return;
    }
  } else {
    /* City-wide: pick store with most ready work */
    stores.sort(function(a,b){ return (b.ready||0)-(a.ready||0); });
    for(var j=0;j<stores.length;j++){
      if((stores[j].ready||0)>0){ target=stores[j]; break; }
    }
    if(!target){
      TN_READY=null;
      if(slip)slip.innerHTML='<span class="empty-note">No ready work on any ledger</span>';
      if(btn)btn.disabled=true;
      return;
    }
  }
  var url="/api/admin/tasks/ready?product="+encodeURIComponent(target.slug)+"&limit=1";
  fetch(url,{cache:"no-store"}).then(function(r){return r.json();}).then(function(j){
    var tasks=j.tasks||j.ready||[];
    if(!tasks.length){
      TN_READY=null;
      if(slip)slip.innerHTML='<span class="empty-note">Ready queue empty for '+esc(target.display||target.slug)+'</span>';
      if(btn)btn.disabled=true;
      return;
    }
    var t=tasks[0];
    TN_READY={id:t.id, project:target.slug, title:t.title||t.id};
    if(slip)slip.innerHTML=
      '<div class="tn-id">'+esc(t.id)+'</div>'+
      '<div class="tn-meta">P'+(t.priority||3)+' · '+esc(target.display||target.slug)+'</div>'+
      '<div class="tn-title">'+esc(t.title||"(untitled)")+'</div>';
    if(btn)btn.disabled=false;
  }).catch(function(){
    TN_READY=null;
    if(slip)slip.innerHTML='<span class="empty-note">Could not load ready queue</span>';
    if(btn)btn.disabled=true;
  });
}
function tnClaim(){
  if(!TN_READY||!TN_READY.id)return;
  var st=$("tnStatus"), btn=$("tnClaimBtn");
  if(btn)btn.disabled=true;
  if(st){ st.className="fw-status"; st.textContent="claiming…"; }
  var author=fwAuthor();
  var body={
    status:"in_progress",
    author:author
  };
  fetch("/api/admin/tasks/"+encodeURIComponent(TN_READY.id),{
    method:"PATCH",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)
  }).then(function(r){return r.json().then(function(d){return {ok:r.ok,d:d};});})
   .then(function(x){
     if(!x.ok||(x.d&&x.d.ok===false)){
       if(st){ st.className="fw-status err";
         st.textContent=(x.d&&x.d.error)||"claim failed"; }
       if(btn)btn.disabled=false;
       return;
     }
     /* Owner marker comment */
     return fetch("/api/admin/tasks/"+encodeURIComponent(TN_READY.id)+"/comments",{
       method:"POST",
       headers:{"Content-Type":"application/json"},
       body:JSON.stringify({
         body:"Owner: "+author+"\\nWorkdir: desk-take-number\\nStart: "+new Date().toISOString()+"\\nPlan:\\n- claimed from desk front window (wl-154)",
         author:author
       })
     }).then(function(){
       var claimedId=TN_READY?TN_READY.id:"";
       if(st){ st.className="fw-status ok"; st.textContent="claimed "+claimedId;
         setTimeout(function(){ if(st&&st.textContent==="claimed "+claimedId){ st.className="fw-status"; st.textContent=""; } }, 5000);
       }
       try{ poll(); }catch(e){}
       try{ if(typeof openWO==="function"&&claimedId) openWO(claimedId); }catch(e){}
       TN_READY=null;
     });
   }).catch(function(){
     if(st){ st.className="fw-status err"; st.textContent="network error — try again"; }
     if(btn)btn.disabled=false;
   });
}
function stampForStatus(st){
  if(st==="backlog")return {txt:"FILED",cls:""};
  if(st==="in_progress")return {txt:"CLAIMED",cls:"amber"};
  if(st==="in_review")return {txt:"SIGN-OFF DUE",cls:""};
  if(st==="done")return {txt:"SIGNED OFF",cls:"green"};
  if(st==="canceled")return {txt:"CANCELED",cls:"amber"};
  return {txt:String(st||"?").toUpperCase(),cls:""};}
function closeBtnHtml(){return '<button class="wo-close" onclick="closeWO()" title="close (esc)">\\u00d7</button>';}
function closeWO(){
  WO_ID=null;WO_TASK=null;$("wo").classList.remove("open");$("scrim").classList.remove("open");
  /* HISTORY LAW: clear sticky ?open= so refresh does not reopen the overlay. */
  try{
    var u=new URL(location.href);
    if(u.searchParams.has("open")){
      u.searchParams.delete("open");
      var qs=u.searchParams.toString();
      history.replaceState({},"",u.pathname+(qs?("?"+qs):"")+u.hash);
    }
  }catch(err){}
}
function openWO(id){
  WO_ID=id; WO_TASK=null;
  $("scrim").classList.add("open"); $("wo").classList.add("open");
  $("woHead").innerHTML='<div class="no">'+esc(id)+'</div>'+
    '<div class="t">pulling the carbon\\u2026</div>'+closeBtnHtml();
  $("woBody").innerHTML='<div class="empty-note">pulling the record\\u2026</div>';
  $("woFoot").innerHTML="";
  fetchWO(id);}
function fetchWO(id){
  fetch("/api/admin/tasks/"+encodeURIComponent(id),{cache:"no-store"})
  .then(function(r){return r.json();}).then(function(d){
    if(WO_ID!==id)return;
    if(!d.ok){$("woBody").innerHTML='<div class="empty-note">no such record</div>';return;}
    renderWO(d.task);})
  .catch(function(){if(WO_ID===id)
    $("woBody").innerHTML='<div class="empty-note">record unreachable \\u2014 try the full page</div>';});}
function attentionKindFor(t){
  var id=String(t.id||"");
  var att=(SCENE&&SCENE.attention)||[];
  for(var i=0;i<att.length;i++){ if(String(att[i].id)===id) return att[i].kind; }
  return "";}
function woActionsHtml(t){
  var labels=t.labels||[];
  var hasFd=labels.some(function(l){return FD_LABELS[l];});
  var kind=attentionKindFor(t);
  var bits=[], gate="";
  if(t.status==="in_review"){
    bits.push('<button type="button" class="primary" data-wo-act="approve">Approve</button>');
    bits.push('<button type="button" data-wo-act="reopen">Reopen</button>');
  }
  if(hasFd){
    bits.push('<button type="button" class="primary" data-wo-act="verdict">Sign verdict</button>');
  }
  if(t.status==="in_progress"||t.status==="in_review"||kind==="stalled"){
    bits.push('<button type="button" class="warn" data-wo-act="release">Release claim</button>');
  }
  if(t.gate_type==="human"){
    gate='<div class="wo-gate"><strong>At the window</strong>'+
      esc(t.gate_note||"Waiting on a person or external event.")+
      '</div>';
    bits.push('<button type="button" data-wo-act="clear-gate">Clear gate</button>');
  } else if(t.gate_type==="timer"){
    gate='<div class="wo-gate"><strong>Embargo</strong>Releases itself'+
      (t.gate_until?' on '+esc(String(t.gate_until).slice(0,10)):'')+
      (t.gate_note?' \\u2014 '+esc(t.gate_note):'')+
      '. No force-clear.</div>';
  }
  if(!bits.length&&!gate) return {gate:"",actions:""};
  return {gate:gate, actions:bits.length?'<div class="wo-actions" id="woActionRow">'+bits.join("")+'</div>':""};}
function renderWO(t){
  WO_TASK=t;
  var st=stampForStatus(t.status);
  $("woHead").innerHTML='<div class="stamp '+st.cls+'">'+esc(st.txt)+'</div>'+
    '<div class="no">'+esc(t.id)+'</div>'+
    '<div class="t">'+esc(t.title||"")+'</div>'+closeBtnHtml();
  var labels=(t.labels||[]).map(function(l){return '<span class="tag">'+esc(l)+'</span>';}).join(" ");
  var acts=woActionsHtml(t);
  var h=acts.gate+acts.actions+
    '<table class="wo-meta">'+
    '<tr><td>priority</td><td>P'+esc(t.priority!=null?t.priority:"3")+'</td></tr>'+
    '<tr><td>routing</td><td>'+(labels||'<span class="dim">none</span>')+'</td></tr>'+
    '<tr><td>filed</td><td>'+esc(t.created_at||"\\u2014")+'</td></tr>'+
    '<tr><td>last touch</td><td>'+esc(ago(t.updated_at)||t.updated_at||"\\u2014")+'</td></tr>'+
    '</table>';
  if(t.description_html)h+='<div class="wo-desc md">'+t.description_html+'</div>';
  else if(t.description)h+='<div class="wo-desc">'+esc(t.description)+'</div>';
  var rels=t.relations||[];
  if(rels.length){
    h+='<div class="wo-rels"><strong>Relations</strong><ul>';
    rels.forEach(function(r){
      var fr=r.from_id||"", to=r.to_id||"", rt=r.relation_type||"";
      h+='<li><a href="/admin/desk?open='+encodeURIComponent(fr)+'">'+esc(fr)+'</a>'+
        ' <span class="dim">'+esc(rt)+'</span> '+
        '<a href="/admin/desk?open='+encodeURIComponent(to)+'">'+esc(to)+'</a></li>';
    });
    h+='</ul></div>';
  }
  var cs=(t.comments||[]).slice().reverse();
  h+='<h2>Day book \\u00b7 '+cs.length+'</h2>';
  if(!cs.length)h+='<div class="empty-note">no entries yet</div>';
  cs.forEach(function(c){h+='<div class="wo-entry"><span class="who">'+signer(c.author)+
    '</span> <span class="dim">\\u00b7 '+esc(ago(c.created_at)||c.created_at||"")+'</span>'+
    '<div class="body">'+esc(c.body||"")+'</div></div>';});
  $("woBody").innerHTML=h;
  $("woFoot").innerHTML='power edits on <a href="/admin/tasks/'+
    esc(t.id)+'">the full record \\u2197</a>';
  $("woSignAs").innerHTML='SIGNED AS '+signer(founderAuthor());
  $("woErr").textContent="";}
function signer(a){
  /* wl-148: aliases are paint, ids are identity — the alias renders,
     the canonical signed id stays visible */
  if(window.FOUNDER&&a===FOUNDER.founder_id&&FOUNDER.founder_alias)
    return esc(FOUNDER.founder_alias)+' <span class="dim">('+esc(a)+')</span>';
  return esc(a||"unsigned");}
function woPostComment(body){
  return fetch("/api/admin/tasks/"+encodeURIComponent(WO_ID)+"/comments",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({body:body,author:founderAuthor()})}).then(function(r){return r.json();});}
function woPatch(payload){
  return fetch("/api/admin/tasks/"+encodeURIComponent(WO_ID),{method:"PATCH",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify(payload)}).then(function(r){return r.json();});}
function woPatchLabels(remove){
  return fetch("/api/admin/tasks/"+encodeURIComponent(WO_ID)+"/labels",{method:"PATCH",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({remove:remove})}).then(function(r){return r.json();});}
function setWoBusy(on){
  var row=$("woActionRow");
  if(row) Array.prototype.forEach.call(row.querySelectorAll("button"),function(b){b.disabled=!!on;});
  var sb=$("woSignBtn"); if(sb)sb.disabled=!!on;}
function afterWoWrite(j, failMsg){
  setWoBusy(false);
  if(!j||!j.ok){$("woErr").textContent=(j&&j.error)||failMsg||"the desk refused";return;}
  $("woNote").value=""; fetchWO(WO_ID); poll();}
function runWoAct(act){
  if(!WO_ID||!WO_TASK)return;
  var note=$("woNote").value.trim();
  $("woErr").textContent=""; setWoBusy(true);
  if(act==="approve"){
    woPatch({status:"done",author:founderAuthor()})
      .then(function(j){afterWoWrite(j,"approve refused \\u2014 close-out may be missing");})
      .catch(function(){setWoBusy(false);$("woErr").textContent="desk unreachable";});
    return;}
  if(act==="reopen"){
    var body=note||"Reopened by founder \\u2014 needs more work";
    woPostComment(body).then(function(j){
      if(!j||!j.ok){afterWoWrite(j,"could not file reopen note");return;}
      return woPatch({status:"in_progress",author:founderAuthor()});
    }).then(function(j){ if(j!==undefined) afterWoWrite(j,"reopen refused"); })
      .catch(function(){setWoBusy(false);$("woErr").textContent="desk unreachable";});
    return;}
  if(act==="verdict"){
    if(!note){setWoBusy(false);$("woErr").textContent="write the verdict in the note first";return;}
    var drop=(WO_TASK.labels||[]).filter(function(l){return FD_LABELS[l];});
    woPostComment(note).then(function(j){
      if(!j||!j.ok){afterWoWrite(j,"verdict note refused");return;}
      return woPatchLabels(drop);
    }).then(function(j){ if(j!==undefined) afterWoWrite(j,"could not drop founder-decision label"); })
      .catch(function(){setWoBusy(false);$("woErr").textContent="desk unreachable";});
    return;}
  if(act==="release"){
    var rel=note
      ? ("Blocked: "+note+"\\nNext step: return to pool for another agent")
      : ("Released by "+founderAuthor()+" \\u2014 returning to backlog");
    woPostComment(rel).then(function(j){
      if(!j||!j.ok){afterWoWrite(j,"release note refused");return;}
      /* Blocked:+Next step: auto-returns via comment lifecycle; bare release needs PATCH */
      if(note){ afterWoWrite(j); return; }
      return woPatch({status:"backlog",author:founderAuthor()});
    }).then(function(j){ if(j!==undefined) afterWoWrite(j,"release refused"); })
      .catch(function(){setWoBusy(false);$("woErr").textContent="desk unreachable";});
    return;}
  if(act==="clear-gate"){
    woPatch({gate_type:"",author:founderAuthor()})
      .then(function(j){afterWoWrite(j,"could not clear gate");})
      .catch(function(){setWoBusy(false);$("woErr").textContent="desk unreachable";});
    return;}
  setWoBusy(false);}
function signWO(){
  if(!WO_ID)return;
  /* wl-150: the desk signs for the founder — whoever clicked IS the founder;
     other identities sign via MCP/CLI, never this chair */
  var body=$("woNote").value.trim(), b=$("woSignBtn");
  if(!body){$("woErr").textContent="nothing to file \\u2014 write the note first";return;}
  b.disabled=true; $("woErr").textContent="";
  woPostComment(body)
  .then(function(j){b.disabled=false;
    if(!j.ok){$("woErr").textContent=j.error||"the desk refused the note";return;}
    $("woNote").value=""; fetchWO(WO_ID); poll();})
  .catch(function(){b.disabled=false;
    $("woErr").textContent="desk unreachable \\u2014 note not filed";});}
document.addEventListener("click",function(e){
  var actBtn=e.target&&e.target.closest?e.target.closest("[data-wo-act]"):null;
  if(actBtn&&$("wo").contains(actBtn)){ e.preventDefault(); runWoAct(actBtn.getAttribute("data-wo-act")); return; }
  var clear=e.target&&e.target.closest?e.target.closest("#clearSkim"):null;
  if(clear){ e.preventDefault(); clearDeskSkim(); return; }
  var readyChip=e.target&&e.target.closest?e.target.closest(".ready-chip[data-ready-slug]"):null;
  if(readyChip&&!readyChip.classList.contains("zero")){
    e.preventDefault(); e.stopPropagation();
    var rs=readyChip.getAttribute("data-ready-slug")||"all";
    if(TRAY_F===rs&&STATUS_F==="backlog"){ setTrayFilter("all",{keepStatus:false}); setStatusFilter(""); }
    else { if(rs!=="all") setTrayFilter(rs,{keepStatus:true}); else setTrayFilter("all",{keepStatus:true});
      setStatusFilter("backlog"); }
    return;}
  var pileEl=e.target&&e.target.closest?e.target.closest(".pile[data-status]"):null;
  if(pileEl){
    e.preventDefault(); e.stopPropagation();
    var hoodForPile=pileEl.closest(".hood[data-slug]");
    var ps=(hoodForPile&&hoodForPile.getAttribute("data-slug"))||"all";
    var pst=pileEl.getAttribute("data-status")||"";
    if(ps&&ps!=="all") setTrayFilter(ps,{keepStatus:true});
    setStatusFilter(STATUS_F===pst&&TRAY_F===ps?"":pst);
    return;}
  var scopeBtn=e.target&&e.target.closest?e.target.closest("button.hood-scope"):null;
  if(scopeBtn){
    e.preventDefault();
    var slug=scopeBtn.getAttribute("data-slug")||"all";
    if(slug==="all") setTrayFilter("all");
    else setTrayFilter(TRAY_F===slug?"all":slug);
    return;}
  var hood=e.target&&e.target.closest?e.target.closest(".hood[data-slug]"):null;
  if(hood&&!(e.target.closest&&e.target.closest("a"))){
    var hs=hood.getAttribute("data-slug")||"all";
    if(hs==="all") setTrayFilter("all");
    else setTrayFilter(TRAY_F===hs?"all":hs);
    return;}
  if(e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;
  var a=e.target&&e.target.closest?e.target.closest('a[href^="/admin/tasks/"], a[href*="/admin/desk?open="]'):null;
  if(a){
    if(a.closest("#woFoot"))return; /* the full-record link is the escape hatch */
    var href=a.getAttribute("href")||"";
    var aid="";
    if(href.indexOf("/admin/desk?open=")>=0){
      try{ aid=new URL(href,location.origin).searchParams.get("open")||""; }
      catch(_){ aid=""; }
    } else {
      aid=href.slice("/admin/tasks/".length).split("?")[0];
    }
    if(!aid)return;
    e.preventDefault(); openWO(decodeURIComponent(aid)); return;}
  /* Whole IN-TRAY slip → drawer (title is the big click target). */
  var form=e.target&&e.target.closest?e.target.closest(".form[data-id]"):null;
  if(form){
    var fid=form.getAttribute("data-id")||"";
    if(fid){ e.preventDefault(); openWO(fid); }
  }});
document.addEventListener("keydown",function(e){if(e.key==="Escape")closeWO();});
/* Office / deep-link: ?open=<id> drawer; ?cabinet=&status= skim (wl-186). */
(function bootOpenFromQuery(){
  try{
    var id=(deskQuery().get("open")||"");
    if(id) openWO(id);
  }catch(err){}
})();
function clearDeskSkim(){
  TRAY_F="all"; STATUS_F=""; STATUS_SKIM=null; STATUS_SKIM_KEY="";
  localStorage.setItem("wl_desk_tray_filter","all");
  syncDeskQuery(); render();
}
function setTrayFilter(v, opts){
  opts=opts||{};
  TRAY_F=v||"all";
  localStorage.setItem("wl_desk_tray_filter",TRAY_F);
  /* Default: cabinet toggle keeps STATUS_F (Office inbound film + pile drills).
     Pass clearStatus:true to drop the station filter. */
  if(opts.clearStatus) STATUS_F="";
  STATUS_SKIM=null; STATUS_SKIM_KEY="";
  syncDeskQuery(); render();
}
function setStatusFilter(st){
  var next=st||"";
  if(next&&!STATUS_LABEL[next]) next="";
  STATUS_F=next;
  STATUS_SKIM=null; STATUS_SKIM_KEY="";
  syncDeskQuery(); render();
}
/* wl-186: station click filters Desk D0 (Board remains via ledger "board" link). */
document.querySelectorAll(".pl-station").forEach(function(btn){
  btn.addEventListener("click",function(){
    var st=btn.getAttribute("data-status")||"";
    if(!st)return;
    setStatusFilter(STATUS_F===st?"":st);
  });});
/* Pin room to real visible viewport (Simple Browser / embedded webviews). */
(function pinRoomShell(){
  function apply(){
    var h=Math.round((window.visualViewport&&visualViewport.height)||innerHeight);
    if(!h) return;
    document.documentElement.style.height=h+"px";
    document.body.style.height=h+"px";
  }
  apply();
  window.addEventListener("resize",apply);
  if(window.visualViewport) visualViewport.addEventListener("resize",apply);
})();
</script>
