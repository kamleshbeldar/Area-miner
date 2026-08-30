const form = document.querySelector('#area-form');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

if (form) {
  const map = L.map('map', {zoomControl:true}).setView([22.8, 79.4], 5);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:19, attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
  const drawn = new L.FeatureGroup().addTo(map);
  map.addControl(new L.Control.Draw({position:'topright', draw:{polygon:{allowIntersection:false,showArea:true},rectangle:true,circle:true,polyline:false,marker:false,circlemarker:false}, edit:{featureGroup:drawn,remove:true}}));
  let selectedLayer = null;

  function serialize() {
    if (!selectedLayer) { document.querySelector('#geometry').value=''; return; }
    let geometry;
    if (selectedLayer instanceof L.Circle) {
      const r = selectedLayer.getRadius();
      geometry={type:'circle',center:[selectedLayer.getLatLng().lat,selectedLayer.getLatLng().lng],radius_m:r};
      if (r < 200) {
        document.querySelector('#estimate').innerHTML=`<small>AREA WARNING</small><strong style="color:#c0392b">Circle radius is only ${r.toFixed(0)} m — too small to capture any businesses. Draw a larger area (at least 0.5 km).</strong>`;
        document.querySelector('#geometry').value='';
        return;
      }
    } else {
      geometry=selectedLayer.toGeoJSON().geometry;
    }
    document.querySelector('#geometry').value=JSON.stringify(geometry); estimate();
  }
  function setLayer(layer) { drawn.clearLayers(); drawn.addLayer(layer); selectedLayer=layer; map.fitBounds(layer.getBounds(),{padding:[25,25]}); serialize(); }
  map.on(L.Draw.Event.CREATED, event => setLayer(event.layer));
  map.on(L.Draw.Event.EDITED, event => { selectedLayer=event.layers.getLayers()[0] || selectedLayer; serialize(); });
  map.on(L.Draw.Event.DELETED, () => { selectedLayer=null; serialize(); document.querySelector('#estimate').innerHTML='<small>AREA PLAN</small><strong>Select an area to calculate the crawl.</strong>'; });

  document.querySelector('#use-view').addEventListener('click', () => setLayer(L.rectangle(map.getBounds())));
  document.querySelector('#find-place').addEventListener('click', async () => {
    const place=document.querySelector('#place-search').value.trim(), button=document.querySelector('#find-place');
    if(!place) return alert('Enter a city or PIN code.');
    button.disabled=true; button.textContent='Finding...';
    const body=new FormData(); body.append('place',place);
    try { const response=await fetch('/geocode',{method:'POST',body}), data=await response.json(); if(!response.ok)throw new Error(data.error); const radius=Math.max(.2,Number(document.querySelector('#place-radius').value)||3)*1000; setLayer(L.circle([data.lat,data.lng],{radius})); map.setZoom(Math.max(map.getZoom(),12)); }
    catch(error){alert(error.message);} finally{button.disabled=false;button.textContent='Find & circle';}
  });

  async function estimate() {
    const geometry=document.querySelector('#geometry').value; if(!geometry)return;
    const body=new FormData(); body.append('geometry',geometry); body.append('coverage',document.querySelector('#coverage').value); body.append('custom_categories',document.querySelector('#custom-categories').value); body.append('grid_spacing_km',document.querySelector('#grid-spacing').value); body.append('max_queries',document.querySelector('#max-queries').value);
    const response=await fetch('/estimate',{method:'POST',body}), data=await response.json();
    const sizeLabel = data.radius_m != null ? `Circle radius: ${data.radius_m >= 1000 ? (data.radius_m/1000).toFixed(1)+' km' : data.radius_m.toFixed(0)+' m'}` : (data.area_km2 != null ? `Area: ${data.area_km2.toFixed(2)} km²` : '');
    document.querySelector('#estimate').innerHTML=response.ok?`<small>AREA PLAN</small><strong>${data.grid_points} grid points x ${data.categories} categories</strong><span>${data.queries} capped Google Maps queries${sizeLabel ? ' — '+sizeLabel : ''}</span>`:`<small>AREA ERROR</small><strong>${esc(data.error)}</strong>`;
  }
  ['coverage','custom-categories','grid-spacing','max-queries'].forEach(id => document.querySelector(`#${id}`).addEventListener('change',estimate));
  form.addEventListener('submit',event => { if(!document.querySelector('#geometry').value){event.preventDefault();alert('First select a circle, rectangle or polygon on the map.');} });
}

const progress = document.querySelector('[data-job-id]');
if (progress) {
  const jobId=progress.dataset.jobId;
  document.querySelector('#cancel').addEventListener('click',()=>fetch(`/cancel/${jobId}`,{method:'POST'}));
  async function poll(){
    try{
      const response=await fetch(`/api/jobs/${jobId}`),job=await response.json();
      document.querySelector('#status').textContent=job.status; document.querySelector('#stage').textContent=job.stage; document.querySelector('#businesses').textContent=job.metrics.businesses; document.querySelector('#places-discovered').textContent=job.metrics.places_discovered; document.querySelector('#discovery-completed').textContent=job.metrics.discovery_completed; document.querySelector('#discovery-total').textContent=job.metrics.discovery_total; document.querySelector('#details-completed').textContent=job.metrics.details_completed; document.querySelector('#details-total').textContent=job.metrics.places_discovered; document.querySelector('#discovery-pending').textContent=job.metrics.discovery_pending; document.querySelector('#details-pending').textContent=job.metrics.details_pending; document.querySelector('#failures').textContent=job.metrics.failures;
      const discoveryPercent=job.metrics.discovery_total?Math.min(100,(job.metrics.discovery_completed/job.metrics.discovery_total)*100):0; const detailPercent=job.metrics.places_discovered?Math.min(100,(job.metrics.details_completed/job.metrics.places_discovered)*100):0; document.querySelector('#discovery-meter-fill').style.width=`${discoveryPercent}%`; document.querySelector('#detail-meter-fill').style.width=`${detailPercent}%`;
      const logs=document.querySelector('#logs'); logs.textContent=job.logs.join('\n'); logs.scrollTop=logs.scrollHeight;
      document.querySelector('#lead-rows').innerHTML=(job.leads||[]).slice(-30).reverse().map(x=>`<tr><td><b>${esc(x.name)}</b></td><td><span class="category">${esc(x.category)}</span></td><td>${esc(x.rating)}</td><td>${esc(x.reviews)}</td><td>${esc(x.phone)}</td><td>${esc(x.address)}</td></tr>`).join('');
      document.querySelector('#resume')?.classList.toggle('hidden',['running','queued','complete'].includes(job.status)); document.querySelector('#retry')?.classList.toggle('hidden',!job.metrics.failures);
      if(['complete','failed','canceled','interrupted'].includes(job.status))return; setTimeout(poll,1800);
    }catch(error){setTimeout(poll,2500);}
  } poll();
}
