"""Exercise the API using an isolated temporary database."""
import base64
import json
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import server

temporary=tempfile.TemporaryDirectory()
server.DB=Path(temporary.name)/'app.db'
server.MEDIA=Path(temporary.name)/'media'
server.init()
httpd=ThreadingHTTPServer(('127.0.0.1',0),server.App)
threading.Thread(target=httpd.serve_forever,daemon=True).start()
BASE = 'http://127.0.0.1:'+str(httpd.server_port)
def call(path, body=None, token=None):
    headers={'Content-Type':'application/json'}
    if token: headers['Authorization']='Bearer '+token
    request=urllib.request.Request(BASE+'/api/'+path, data=json.dumps(body).encode() if body is not None else None,headers=headers)
    with urllib.request.urlopen(request) as response: return json.load(response)

email='smoke-'+__import__('secrets').token_hex(4)+'@example.test'
call('register',{'name':'Smoke Citizen','email':email,'password':'SafeTestPassword123!'})
citizen=call('login',{'email':email,'password':'SafeTestPassword123!'})['token']
report=call('reports',{'registration':'JK02AB1234','make':'Mahindra','model':'XUV700','colour':'black','theft_date':'2026-11-20','location':'Demo Location A','details':'Smoke test'},citizen)
call('complaints',{'title':'Test complaint','description':'Delete with account'},citizen)
call('reports/'+str(report['id'])+'/evidence',{'mime':'image/png','data':base64.b64encode(b'\x89PNG\r\n\x1a\n').decode()},citizen)
evidence=call('reports',token=citizen)[0]['evidence']
assert (server.MEDIA/evidence).exists()
assert len(call('reports',token=citizen))==1
officer=call('login',{'email':'officer@vehiclewatch.local','password':'OfficerDemo123!'})['token']
log=json.loads((Path(__file__).parent/'web'/'sample-log.json').read_text())
log['video_id']='SMOKE-'+str(report['id'])
log['detections'][0]['candidate_matches'][0]['stolen_vehicle_profile_id']=report['profile_id']
assert call('videos',{'log':log},officer)['alerts_created']==0
assert call('reports/'+str(report['id']),{'status':'Verified'},officer)['alerts_created']==3
video_bytes=(Path(__file__).parent/'web'/'cctv1.mp4').read_bytes()
request=urllib.request.Request(BASE+'/api/demo-upload',data=video_bytes,headers={'Authorization':'Bearer '+officer,'X-Filename':'cctv1.mp4','Content-Type':'video/mp4'})
with urllib.request.urlopen(request) as response: demo=json.load(response)
assert demo['video_id']=='VID-001' and demo['alerts_created']==3
package=call('videos/VID-001',token=officer)
assert package['original']=='VID-001-original.mp4' and package['inference']=='VID-001-inference.mp4'
assert (server.MEDIA/package['original']).read_bytes()==video_bytes
assert (server.MEDIA/package['inference']).exists()
playback=urllib.request.Request(BASE+'/media/'+package['inference'],headers={'Authorization':'Bearer '+officer})
with urllib.request.urlopen(playback) as response:
    assert response.headers['Content-Type']=='video/mp4'
    assert response.read()==(Path(__file__).parent/'demo_backend'/'cctv1inference.mp4').read_bytes()
unknown=urllib.request.Request(BASE+'/api/demo-upload',data=video_bytes,headers={'Authorization':'Bearer '+officer,'X-Filename':'missing.mp4','Content-Type':'video/mp4'})
try:
    urllib.request.urlopen(unknown)
    raise AssertionError('Missing demo package was accepted')
except urllib.error.HTTPError as error:
    assert error.code==404
alerts=call('alerts',token=officer)
own=[a for a in alerts if a['report_id']==report['id']]
assert {a['urgency'] for a in own}=={'Highest urgency','High risk','Low risk'}
assert own[0]['urgency']=='Highest urgency'
alert=own[0]
rto='plate,manufacturer,model,colour\nJK02EF4321,Maruti,Alto K10,white\nJK02AB1234,Mahindra,XUV700,black\n'
assert call('rto',{'format':'csv','text':rto},officer)['imported']==2
assert call('rto',token=officer)['count']==2
rechecked=call('alerts',token=officer)
changed=next(a for a in rechecked if a['report_id']==report['id'] and a['detection_id']=='DET-004')
assert changed['urgency']=='High risk' and changed['lookup_source']=='Synthetic RTO registry'
call('alerts/'+str(alert['id']),{'action':'Confirmed','note':'Smoke review'},officer)
call('alerts/'+str(alert['id']),{'action':'notify'},officer)
assert len(call('notifications',token=citizen))==1
try:
    call('users/delete',{'email':email,'confirmation':'DELETE'},citizen)
    raise AssertionError('Citizen could delete through officer endpoint')
except urllib.error.HTTPError as error:
    assert error.code==403
deleted=call('users/delete',{'email':email,'confirmation':'DELETE'},officer)
assert deleted['items']=={'reports':1,'complaints':1,'alerts':6}
assert all(a['report_id']!=report['id'] for a in call('alerts',token=officer))
assert len(call('videos',token=officer))==2
assert not (server.MEDIA/evidence).exists()
with server.conn() as db:
    assert db.execute('SELECT count(*) FROM notifications').fetchone()[0]==0
try:
    call('me',token=citizen)
    raise AssertionError('Deleted citizen session remained active')
except urllib.error.HTTPError as error:
    assert error.code==403
print('PASS: demo video upload, precomputed result mapping, alerts, RTO import, review, notification, deletion')
httpd.shutdown()
httpd.server_close()
temporary.cleanup()
