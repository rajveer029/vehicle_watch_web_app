import base64, csv, hashlib, hmac, io, json, mimetypes, re, secrets, shutil, sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT=Path(__file__).resolve().parent
DB=ROOT/'data'/'app.db'
WEB=ROOT/'web'
MEDIA=ROOT/'data'/'media'
DEMO=ROOT/'demo_backend'
MAX_DEMO_VIDEO=250*1024*1024
def stamp(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def pw(value,salt=None):
    salt=salt or secrets.token_hex(16)
    return salt+':'+hashlib.pbkdf2_hmac('sha256',value.encode(),bytes.fromhex(salt),250000).hex()
@contextmanager
def conn():
    db=sqlite3.connect(DB)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
def allrows(cursor): return [dict(x) for x in cursor]
def normalized(value): return re.sub(r'[^A-Z0-9]','',str(value or '').upper())
def classify(d,report,lookup_override=None):
    observed=normalized((d.get('registration') or {}).get('text'))
    stolen_plate=normalized(report['registration'])
    lookup=lookup_override if lookup_override is not None else (d.get('registration_comparison') or {})
    registered=lookup.get('registered_vehicle') or {}
    if observed and observed==stolen_plate:
        return 'Highest urgency','Exact plate matches the stolen vehicle report',lookup.get('lookup_status','not_provided'),registered
    if not observed:
        return 'Needs lookup','Plate could not be read',lookup.get('lookup_status','not_provided'),registered
    if lookup.get('lookup_status')!='completed' or not registered:
        return 'Needs lookup','Registration lookup is unavailable or incomplete',lookup.get('lookup_status','not_provided'),registered
    vehicle=d.get('vehicle') or {}
    compared=[]
    for observed_key,registered_key in (('manufacturer','manufacturer'),('model','model'),('colour','colour')):
        if registered.get(registered_key) and vehicle.get(observed_key):
            compared.append((observed_key,normalized(vehicle[observed_key]),normalized(registered[registered_key])))
    mismatched=[label for label,actual,expected in compared if actual!=expected]
    if mismatched or lookup.get('mismatch_detected') is True:
        reason='Registration belongs to a different '+', '.join(mismatched) if mismatched else 'Registration lookup reports a vehicle mismatch'
        return 'High risk',reason,'completed',registered
    if len(compared)==3:
        return 'Low risk','Plate registration matches the observed make, model and colour','completed',registered
    return 'Needs lookup','Registration lookup lacks complete vehicle details','completed',registered
def init():
    MEDIA.mkdir(parents=True,exist_ok=True)
    with conn() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,name TEXT,email TEXT UNIQUE,password TEXT,role TEXT,token TEXT);
        CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY,citizen_id INTEGER,profile_id TEXT UNIQUE,registration TEXT,make TEXT,model TEXT,colour TEXT,theft_date TEXT,location TEXT,details TEXT,evidence TEXT,status TEXT DEFAULT 'Pending verification',monitoring INTEGER DEFAULT 0,created_at TEXT);
        CREATE TABLE IF NOT EXISTS complaints(id INTEGER PRIMARY KEY,citizen_id INTEGER,title TEXT,description TEXT,status TEXT DEFAULT 'Submitted',created_at TEXT);
        CREATE TABLE IF NOT EXISTS videos(id TEXT PRIMARY KEY,log TEXT,original TEXT,inference TEXT,imported_at TEXT);
        CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY,video_id TEXT,detection_id TEXT,report_id INTEGER,timestamp REAL,plate TEXT,vehicle TEXT,similarity REAL,status TEXT DEFAULT 'Pending review',note TEXT,reviewer_id INTEGER,created_at TEXT,UNIQUE(video_id,detection_id,report_id));
        CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY,citizen_id INTEGER,alert_id INTEGER UNIQUE,message TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor_id INTEGER,action TEXT,subject TEXT,detail TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS rto_registry(plate TEXT PRIMARY KEY,manufacturer TEXT NOT NULL,model TEXT NOT NULL,colour TEXT NOT NULL,updated_at TEXT NOT NULL);
        ''')
        existing={row['name'] for row in db.execute('PRAGMA table_info(alerts)')}
        for name,kind in (('urgency','TEXT'),('reason','TEXT'),('lookup_status','TEXT'),('registered_vehicle','TEXT'),('lookup_source','TEXT')):
            if name not in existing: db.execute(f'ALTER TABLE alerts ADD COLUMN {name} {kind}')
        if not db.execute("SELECT 1 FROM users WHERE role='officer'").fetchone():
            db.execute('INSERT INTO users(name,email,password,role) VALUES(?,?,?,?)',('Demo Officer','officer@vehiclewatch.local',pw('OfficerDemo123!'),'officer'))
        officer=db.execute("SELECT id FROM users WHERE role='officer' LIMIT 1").fetchone()
        for video in db.execute('SELECT id,log FROM videos').fetchall():
            match(db,video['id'],json.loads(video['log']),officer)
def audit(db,u,action,subject,detail=''):
    db.execute('INSERT INTO audit(actor_id,action,subject,detail,created_at) VALUES(?,?,?,?,?)',(u['id'],action,str(subject),detail,stamp()))
def delete_citizen_data(db,citizen,officer):
    reports=db.execute('SELECT id,profile_id FROM reports WHERE citizen_id=?',(citizen['id'],)).fetchall()
    report_ids=[r['id'] for r in reports]
    complaint_ids=[r['id'] for r in db.execute('SELECT id FROM complaints WHERE citizen_id=?',(citizen['id'],))]
    alert_ids=[a['id'] for rid in report_ids for a in db.execute('SELECT id FROM alerts WHERE report_id=?',(rid,))]
    for aid in alert_ids:
        db.execute('DELETE FROM notifications WHERE alert_id=?',(aid,))
        db.execute("DELETE FROM audit WHERE subject=? AND action LIKE 'alert_%'",(str(aid),))
        db.execute('DELETE FROM alerts WHERE id=?',(aid,))
    db.execute('DELETE FROM notifications WHERE citizen_id=?',(citizen['id'],))
    for r in reports:
        db.execute("DELETE FROM audit WHERE subject IN (?,?) AND action IN ('report_created','report_status','evidence_uploaded')",(str(r['id']),r['profile_id']))
        db.execute('DELETE FROM reports WHERE id=?',(r['id'],))
    for cid in complaint_ids:
        db.execute("DELETE FROM audit WHERE subject=? AND action IN ('complaint_created','complaint_status')",(str(cid),))
        db.execute('DELETE FROM complaints WHERE id=?',(cid,))
    db.execute('DELETE FROM audit WHERE actor_id=?',(citizen['id'],))
    db.execute('DELETE FROM users WHERE id=?',(citizen['id'],))
    for rid in report_ids:
        for path in MEDIA.glob(f'{rid}-*'):
            if path.suffix.lower() in ('.jpg','.png','.pdf') and path.is_file(): path.unlink()
    audit(db,officer,'citizen_data_deleted',citizen['id'],f'{len(reports)} reports, {len(complaint_ids)} complaints, {len(alert_ids)} alerts')
    return {'reports':len(reports),'complaints':len(complaint_ids),'alerts':len(alert_ids)}
def parse_rto_import(data):
    text=data.get('text')
    if not isinstance(text,str) or not text.strip(): raise ValueError('Select a CSV or JSON file')
    if data.get('format')=='csv':
        reader=csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
        if not reader.fieldnames: raise ValueError('CSV header is missing')
        records=[{str(k or '').strip().lower():v for k,v in row.items()} for row in reader]
    elif data.get('format')=='json':
        records=json.loads(text)
        if isinstance(records,dict): records=records.get('vehicles')
    else: raise ValueError('Use a CSV or JSON file')
    if not isinstance(records,list) or not records or len(records)>10000: raise ValueError('Provide 1 to 10,000 vehicle records')
    result=[]; seen=set()
    for index,item in enumerate(records,1):
        if not isinstance(item,dict): raise ValueError(f'Row {index} must be an object')
        item={str(k).strip().lower():v for k,v in item.items()}
        plate=normalized(item.get('plate') or item.get('registration'))
        manufacturer=str(item.get('manufacturer') or item.get('make') or '').strip()
        model=str(item.get('model') or '').strip()
        colour=str(item.get('colour') or item.get('color') or '').strip()
        if not (4<=len(plate)<=16) or not all((manufacturer,model,colour)) or any(len(x)>100 for x in (manufacturer,model,colour)):
            raise ValueError(f'Row {index} needs plate, manufacturer, model and colour')
        if plate in seen: raise ValueError(f'Duplicate plate in file: {plate}')
        seen.add(plate); result.append((plate,manufacturer,model,colour))
    return result
def match(db,video_id,log,u):
    count=0; seen_tracks=set()
    registry_active=bool(db.execute('SELECT 1 FROM rto_registry LIMIT 1').fetchone())
    for d in log.get('detections',[]):
        vehicle=d.get('vehicle') or {}
        if not all(vehicle.get(k) for k in ('manufacturer','model','colour')): continue
        observed_plate=normalized((d.get('registration') or {}).get('text'))
        registration=db.execute('SELECT manufacturer,model,colour FROM rto_registry WHERE plate=?',(observed_plate,)).fetchone() if registry_active and observed_plate else None
        lookup_override=({'lookup_status':'completed','registered_vehicle':dict(registration)} if registration else {'lookup_status':'not_found','registered_vehicle':{}}) if registry_active else None
        source='Synthetic RTO registry' if registry_active else 'Inference JSON'
        for r in db.execute("SELECT * FROM reports WHERE status='Verified' AND monitoring=1").fetchall():
            if any(normalized(vehicle[k])!=normalized(r[report_key]) for k,report_key in (('manufacturer','make'),('model','model'),('colour','colour'))): continue
            track_key=(r['id'],d.get('track_id') or d['detection_id'])
            if track_key in seen_tracks: continue
            seen_tracks.add(track_key)
            urgency,reason,lookup_status,registered=classify(d,r,lookup_override)
            candidate=next((c for c in d.get('candidate_matches',[]) if c.get('stolen_vehicle_profile_id')==r['profile_id']),{})
            existing=db.execute('SELECT id FROM alerts WHERE video_id=? AND detection_id=? AND report_id=?',(video_id,d['detection_id'],r['id'])).fetchone()
            db.execute('''INSERT INTO alerts(video_id,detection_id,report_id,timestamp,plate,vehicle,similarity,urgency,reason,lookup_status,registered_vehicle,lookup_source,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(video_id,detection_id,report_id) DO UPDATE SET urgency=excluded.urgency,reason=excluded.reason,lookup_status=excluded.lookup_status,registered_vehicle=excluded.registered_vehicle,lookup_source=excluded.lookup_source,plate=excluded.plate,vehicle=excluded.vehicle''',
                (video_id,d['detection_id'],r['id'],d['timestamp_seconds'],(d.get('registration') or {}).get('text'),' '.join(str(vehicle.get(k,'')) for k in ('colour','manufacturer','model')).strip(),candidate.get('visual_similarity'),urgency,reason,lookup_status,json.dumps(registered),source,stamp()))
            if not existing: count+=1
    if count: audit(db,u,'alerts_created',video_id,str(count))
    return count
class App(BaseHTTPRequestHandler):
    def response(self,code,obj):
        b=json.dumps(obj).encode(); self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0'))
        if n>12_000_000: raise ValueError('File is too large')
        return json.loads(self.rfile.read(n) or b'{}')
    def user(self,db):
        token=self.headers.get('Authorization','').removeprefix('Bearer ')
        return db.execute('SELECT id,name,email,role FROM users WHERE token=?',(token,)).fetchone() if token else None
    def demo_upload(self):
        temp=None
        try:
            with conn() as db:
                officer=self.user(db)
                if not officer or officer['role']!='officer': raise PermissionError('Officer access required')
                officer=dict(officer)
            filename=unquote(self.headers.get('X-Filename','')).lower()
            if not re.fullmatch(r'[a-z0-9_-]+\.mp4',filename): raise ValueError('Use a simple MP4 filename such as cctv1.mp4')
            stem=filename[:-4]
            log_file=DEMO/(stem+'.json')
            inference_file=DEMO/(stem+'inference.mp4')
            if not log_file.is_file() or not inference_file.is_file():
                return self.response(404,{'error':f'No precomputed demo package for {filename}. Add {stem}.json and {stem}inference.mp4 to demo_backend.'})
            log=json.loads(log_file.read_text(encoding='utf-8'))
            video_id=str(log.get('video_id',''))
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',video_id) or not isinstance(log.get('detections'),list): raise ValueError('Demo JSON has an invalid video ID or detections')
            size=int(self.headers.get('Content-Length','0'))
            if size<12 or size>MAX_DEMO_VIDEO: raise ValueError('MP4 must be between 12 bytes and 250 MB')
            first=self.rfile.read(min(size,1024*1024))
            if len(first)<12 or first[4:8]!=b'ftyp': raise ValueError('Uploaded file is not an MP4')
            temp=MEDIA/(video_id+'-'+secrets.token_hex(8)+'.upload')
            with temp.open('wb') as output:
                output.write(first)
                remaining=size-len(first)
                while remaining:
                    chunk=self.rfile.read(min(1024*1024,remaining))
                    if not chunk: raise ValueError('Upload ended before the complete video arrived')
                    output.write(chunk); remaining-=len(chunk)
            original_name=video_id+'-original.mp4'
            inference_name=video_id+'-inference.mp4'
            temp.replace(MEDIA/original_name); temp=None
            shutil.copyfile(inference_file,MEDIA/inference_name)
            with conn() as db:
                db.execute('''INSERT INTO videos(id,log,original,inference,imported_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET log=excluded.log,original=excluded.original,inference=excluded.inference,imported_at=excluded.imported_at''',
                    (video_id,json.dumps(log),original_name,inference_name,stamp()))
                count=match(db,video_id,log,officer)
                audit(db,officer,'demo_video_uploaded',video_id,filename)
            return self.response(201,{'video_id':video_id,'detections':len(log['detections']),'alerts_created':count,'demo':True})
        except PermissionError as error: self.response(403,{'error':str(error)})
        except (ValueError,TypeError,json.JSONDecodeError) as error: self.response(400,{'error':str(error)})
        except Exception as error:
            print('Demo upload error:',repr(error)); self.response(500,{'error':'Demo upload failed'})
        finally:
            if temp and temp.exists(): temp.unlink()
    def handle_api(self,method):
        p=urlparse(self.path).path; d=self.body() if method=='POST' else {}
        try:
            with conn() as db:
                if p=='/api/register' and method=='POST':
                    name=str(d.get('name','')).strip(); email=str(d.get('email','')).strip().lower(); password=str(d.get('password',''))
                    if len(name)<2 or '@' not in email or len(password)<10: raise ValueError('Enter a name, email and password of at least 10 characters')
                    try: db.execute("INSERT INTO users(name,email,password,role) VALUES(?,?,?,'citizen')",(name,email,pw(password)))
                    except sqlite3.IntegrityError: raise ValueError('Email already registered')
                    return self.response(201,{'ok':True})
                if p=='/api/login' and method=='POST':
                    u=db.execute('SELECT * FROM users WHERE email=?',(str(d.get('email','')).lower(),)).fetchone()
                    if not u or not hmac.compare_digest(pw(str(d.get('password','')),u['password'].split(':')[0]),u['password']): raise PermissionError('Invalid credentials')
                    token=secrets.token_urlsafe(32); db.execute('UPDATE users SET token=? WHERE id=?',(token,u['id'])); audit(db,u,'login','account')
                    return self.response(200,{'token':token,'user':{k:u[k] for k in ('id','name','email','role')}})
                u=self.user(db)
                if not u: raise PermissionError('Sign in required')
                officer=u['role']=='officer'
                if p=='/api/me': return self.response(200,dict(u))
                if p=='/api/logout' and method=='POST':
                    db.execute('UPDATE users SET token=NULL WHERE id=?',(u['id'],)); return self.response(200,{'ok':True})
                if p=='/api/users/delete' and method=='POST':
                    if not officer: raise PermissionError('Officer access required')
                    email=str(d.get('email','')).strip().lower()
                    if d.get('confirmation')!='DELETE' or not email: raise ValueError('Enter the citizen email and type DELETE to confirm')
                    target=db.execute("SELECT id,email FROM users WHERE email=? AND role='citizen'",(email,)).fetchone()
                    if not target: return self.response(404,{'error':'Citizen account not found'})
                    deleted=delete_citizen_data(db,target,u)
                    return self.response(200,{'deleted':True,'items':deleted})
                if p=='/api/summary':
                    if officer: return self.response(200,{'reports':db.execute("SELECT count(*) FROM reports WHERE status='Pending verification'").fetchone()[0],'alerts':db.execute("SELECT count(*) FROM alerts WHERE status='Pending review'").fetchone()[0],'complaints':db.execute("SELECT count(*) FROM complaints WHERE status='Submitted'").fetchone()[0],'videos':db.execute('SELECT count(*) FROM videos').fetchone()[0]})
                    return self.response(200,{'reports':db.execute('SELECT count(*) FROM reports WHERE citizen_id=?',(u['id'],)).fetchone()[0],'complaints':db.execute('SELECT count(*) FROM complaints WHERE citizen_id=?',(u['id'],)).fetchone()[0],'notifications':db.execute('SELECT count(*) FROM notifications WHERE citizen_id=?',(u['id'],)).fetchone()[0]})
                if p=='/api/reports':
                    if method=='GET': return self.response(200,allrows(db.execute('SELECT r.*,u.name citizen FROM reports r JOIN users u ON u.id=r.citizen_id'+(' ORDER BY r.id DESC' if officer else ' WHERE r.citizen_id=? ORDER BY r.id DESC'),() if officer else (u['id'],))))
                    if officer: raise PermissionError('Citizen only')
                    keys=('registration','make','model','colour','theft_date','location','details'); vals=[str(d.get(k,'')).strip() for k in keys]
                    if not all(vals): raise ValueError('Complete every report field')
                    cur=db.execute('INSERT INTO reports(citizen_id,profile_id,registration,make,model,colour,theft_date,location,details,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(u['id'],'TMP-'+secrets.token_hex(8),*vals,stamp()))
                    profile=f'SVP-{cur.lastrowid:04d}'; db.execute('UPDATE reports SET profile_id=? WHERE id=?',(profile,cur.lastrowid)); audit(db,u,'report_created',profile)
                    return self.response(201,{'id':cur.lastrowid,'profile_id':profile})
                if p.startswith('/api/reports/') and method=='POST':
                    parts=p.split('/'); rid=int(parts[3]); r=db.execute('SELECT * FROM reports WHERE id=?',(rid,)).fetchone()
                    if not r: return self.response(404,{'error':'Report not found'})
                    if len(parts)==5 and parts[4]=='evidence':
                        if r['citizen_id']!=u['id']: raise PermissionError('Access denied')
                        ext={'image/jpeg':'.jpg','image/png':'.png','application/pdf':'.pdf'}.get(d.get('mime'))
                        if not ext: raise ValueError('Use JPG, PNG or PDF')
                        raw=base64.b64decode(d.get('data',''),validate=True)
                        if len(raw)>8_000_000: raise ValueError('Evidence must be below 8 MB')
                        filename=f'{rid}-{secrets.token_hex(6)}{ext}'; (MEDIA/filename).write_bytes(raw)
                        db.execute('UPDATE reports SET evidence=? WHERE id=?',(filename,rid)); audit(db,u,'evidence_uploaded',rid); return self.response(200,{'ok':True})
                    if not officer: raise PermissionError('Officer only')
                    status=d.get('status')
                    if status not in ('Verified','Rejected','Recovered','Monitoring paused'): raise ValueError('Invalid status')
                    db.execute('UPDATE reports SET status=?,monitoring=? WHERE id=?',(status,int(status=='Verified'),rid)); audit(db,u,'report_status',rid,status)
                    if status!='Verified':
                        db.execute("UPDATE alerts SET status='Dismissed',note='Report no longer active',reviewer_id=? WHERE report_id=? AND status='Pending review'",(u['id'],rid))
                    count=sum(match(db,v['id'],json.loads(v['log']),u) for v in db.execute('SELECT * FROM videos')) if status=='Verified' else 0
                    return self.response(200,{'ok':True,'alerts_created':count})
                if p=='/api/complaints':
                    if method=='GET': return self.response(200,allrows(db.execute('SELECT c.*,u.name citizen FROM complaints c JOIN users u ON u.id=c.citizen_id'+(' ORDER BY c.id DESC' if officer else ' WHERE c.citizen_id=? ORDER BY c.id DESC'),() if officer else (u['id'],))))
                    if officer: raise PermissionError('Citizen only')
                    title=str(d.get('title','')).strip(); description=str(d.get('description','')).strip()
                    if not title or not description: raise ValueError('Title and description required')
                    cur=db.execute('INSERT INTO complaints(citizen_id,title,description,created_at) VALUES(?,?,?,?)',(u['id'],title,description,stamp())); audit(db,u,'complaint_created',cur.lastrowid); return self.response(201,{'id':cur.lastrowid})
                if p.startswith('/api/complaints/') and method=='POST':
                    if not officer: raise PermissionError('Officer only')
                    status=d.get('status')
                    if status not in ('In review','Resolved','Closed'): raise ValueError('Invalid status')
                    cid=int(p.split('/')[3]); db.execute('UPDATE complaints SET status=? WHERE id=?',(status,cid)); audit(db,u,'complaint_status',cid,status); return self.response(200,{'ok':True})
                if p=='/api/rto':
                    if not officer: raise PermissionError('Officer only')
                    if method=='GET':
                        return self.response(200,{'count':db.execute('SELECT count(*) FROM rto_registry').fetchone()[0],'vehicles':allrows(db.execute('SELECT plate,manufacturer,model,colour,updated_at FROM rto_registry ORDER BY plate LIMIT 50'))})
                    records=parse_rto_import(d)
                    db.executemany('INSERT INTO rto_registry(plate,manufacturer,model,colour,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(plate) DO UPDATE SET manufacturer=excluded.manufacturer,model=excluded.model,colour=excluded.colour,updated_at=excluded.updated_at',[(plate,make,model,colour,stamp()) for plate,make,model,colour in records])
                    reclassified=0
                    for video in db.execute('SELECT id,log FROM videos').fetchall():
                        match(db,video['id'],json.loads(video['log']),u)
                        reclassified+=db.execute('SELECT count(*) FROM alerts WHERE video_id=?',(video['id'],)).fetchone()[0]
                    audit(db,u,'rto_import',len(records),f'{reclassified} alerts checked')
                    return self.response(200,{'imported':len(records),'registry_count':db.execute('SELECT count(*) FROM rto_registry').fetchone()[0],'alerts_checked':reclassified})
                if p=='/api/videos':
                    if not officer: raise PermissionError('Officer only')
                    if method=='GET': return self.response(200,[{'id':v['id'],'metadata':json.loads(v['log']).get('video_metadata',{}),'original':v['original'],'inference':v['inference'],'imported_at':v['imported_at']} for v in db.execute('SELECT * FROM videos ORDER BY imported_at DESC')])
                    log=d.get('log')
                    if not isinstance(log,dict) or not isinstance(log.get('detections'),list): raise ValueError('Select a valid inference JSON log')
                    vid=str(log.get('video_id',''))
                    if not vid or len(vid)>80 or not all(x.isalnum() or x in '-_' for x in vid): raise ValueError('Invalid video ID')
                    duration=float((log.get('video_metadata') or {}).get('duration_seconds') or 0); seen=set()
                    for det in log['detections']:
                        did=det.get('detection_id'); ts=det.get('timestamp_seconds')
                        if not did or did in seen or not isinstance(ts,(int,float)) or ts<0 or (duration and ts>duration): raise ValueError('Invalid detection ID or timestamp')
                        seen.add(did)
                    db.execute('INSERT OR REPLACE INTO videos(id,log,original,inference,imported_at) VALUES(?,?,COALESCE((SELECT original FROM videos WHERE id=?),NULL),COALESCE((SELECT inference FROM videos WHERE id=?),NULL),?)',(vid,json.dumps(log),vid,vid,stamp()))
                    count=match(db,vid,log,u); audit(db,u,'video_imported',vid); return self.response(201,{'id':vid,'alerts_created':count})
                if p.startswith('/api/videos/'):
                    if not officer: raise PermissionError('Officer only')
                    parts=p.split('/'); vid=parts[3]; v=db.execute('SELECT * FROM videos WHERE id=?',(vid,)).fetchone()
                    if not v: return self.response(404,{'error':'Video not found'})
                    if method=='GET': return self.response(200,{**dict(v),'log':json.loads(v['log'])})
                    if len(parts)==5 and parts[4]=='file':
                        kind=d.get('kind')
                        if kind not in ('original','inference'): raise ValueError('Invalid video type')
                        raw=base64.b64decode(d.get('data',''),validate=True)
                        if len(raw)>10_000_000: raise ValueError('For larger MP4 files, copy them into data/media and update the database')
                        name=vid+'-'+kind+'.mp4'; (MEDIA/name).write_bytes(raw); db.execute(f'UPDATE videos SET {kind}=? WHERE id=?',(name,vid)); audit(db,u,'video_attached',vid,kind); return self.response(200,{'ok':True})
                if p=='/api/alerts':
                    if not officer: raise PermissionError('Officer only')
                    return self.response(200,allrows(db.execute("SELECT a.*,r.profile_id,r.registration,r.make,r.model,r.colour,r.location,u.name citizen FROM alerts a JOIN reports r ON r.id=a.report_id JOIN users u ON u.id=r.citizen_id ORDER BY CASE a.urgency WHEN 'Highest urgency' THEN 0 WHEN 'High risk' THEN 1 WHEN 'Needs lookup' THEN 2 ELSE 3 END,a.id DESC")))
                if p.startswith('/api/alerts/') and method=='POST':
                    if not officer: raise PermissionError('Officer only')
                    aid=int(p.split('/')[3]); a=db.execute('SELECT a.*,r.citizen_id FROM alerts a JOIN reports r ON r.id=a.report_id WHERE a.id=?',(aid,)).fetchone()
                    if not a: return self.response(404,{'error':'Alert not found'})
                    action=d.get('action'); note=str(d.get('note','')).strip()
                    if action=='notify':
                        if a['status']!='Confirmed': raise ValueError('Confirm sighting before notifying')
                        db.execute('INSERT OR IGNORE INTO notifications(citizen_id,alert_id,message,created_at) VALUES(?,?,?,?)',(a['citizen_id'],aid,'An officer reviewed a possible sighting related to your vehicle report. Contact the investigating station for next steps.',stamp()))
                    elif action in ('Confirmed','Dismissed'):
                        if action=='Dismissed' and not note: raise ValueError('A dismissal reason is required')
                        db.execute('UPDATE alerts SET status=?,note=?,reviewer_id=? WHERE id=?',(action,note,u['id'],aid))
                    else: raise ValueError('Invalid action')
                    audit(db,u,'alert_'+action.lower(),aid,note); return self.response(200,{'ok':True})
                if p=='/api/notifications': return self.response(200,allrows(db.execute('SELECT * FROM notifications WHERE citizen_id=? ORDER BY id DESC',(u['id'],))))
                if p=='/api/audit':
                    if not officer: raise PermissionError('Officer only')
                    return self.response(200,allrows(db.execute('SELECT a.*,u.name actor FROM audit a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 100')))
                return self.response(404,{'error':'Not found'})
        except PermissionError as e: self.response(403,{'error':str(e)})
        except (ValueError,KeyError,TypeError) as e: self.response(400,{'error':str(e)})
        except Exception as e: print(repr(e)); self.response(500,{'error':'Server error'})
    def static(self):
        p=urlparse(self.path).path
        if p.startswith('/evidence/'):
            filename=Path(p).name
            with conn() as db:
                u=self.user(db)
                if not u: return self.response(403,{'error':'Sign in required'})
                report=db.execute('SELECT citizen_id FROM reports WHERE evidence=?',(filename,)).fetchone()
                if not report or (u['role']!='officer' and report['citizen_id']!=u['id']): return self.response(403,{'error':'Access denied'})
            path=MEDIA/filename
        elif p.startswith('/media/'):
            with conn() as db:
                u=self.user(db)
                if not u or u['role']!='officer': return self.response(403,{'error':'Officer only'})
            path=MEDIA/Path(p).name
        else: path=WEB/('index.html' if p=='/' else Path(p).name)
        if not path.is_file(): return self.response(404,{'error':'Not found'})
        self.send_response(200)
        self.send_header('Content-Type',mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(path.stat().st_size))
        self.end_headers()
        with path.open('rb') as source: shutil.copyfileobj(source,self.wfile,1024*1024)
    def do_GET(self): self.handle_api('GET') if self.path.startswith('/api/') else self.static()
    def do_POST(self): self.demo_upload() if urlparse(self.path).path=='/api/demo-upload' else self.handle_api('POST')
if __name__=='__main__':
    init(); print('VehicleWatch: http://127.0.0.1:8000'); ThreadingHTTPServer(('127.0.0.1',8000),App).serve_forever()
