import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import server
from server import classify, match, parse_rto_import

SAMPLE=json.loads((Path(__file__).parent/'web'/'sample-log.json').read_text())

class UrgencyTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
        CREATE TABLE reports(id INTEGER PRIMARY KEY,profile_id TEXT,registration TEXT,make TEXT,model TEXT,colour TEXT,status TEXT,monitoring INTEGER);
        CREATE TABLE alerts(id INTEGER PRIMARY KEY,video_id TEXT,detection_id TEXT,report_id INTEGER,timestamp REAL,plate TEXT,vehicle TEXT,similarity REAL,urgency TEXT,reason TEXT,lookup_status TEXT,registered_vehicle TEXT,lookup_source TEXT,created_at TEXT,UNIQUE(video_id,detection_id,report_id));
        CREATE TABLE audit(id INTEGER PRIMARY KEY,actor_id INTEGER,action TEXT,subject TEXT,detail TEXT,created_at TEXT);
        CREATE TABLE rto_registry(plate TEXT PRIMARY KEY,manufacturer TEXT,model TEXT,colour TEXT,updated_at TEXT);
        INSERT INTO reports VALUES(1,'SVP-0001','JK02AB1234','Mahindra','XUV700','black','Verified',1);
        ''')
        self.report=self.db.execute('SELECT * FROM reports').fetchone()
    def tearDown(self): self.db.close()
    def test_three_priorities_and_visual_filter(self):
        detections=SAMPLE['detections']
        self.assertEqual(classify(detections[0],self.report)[0],'High risk')
        self.assertEqual(classify(detections[2],self.report)[0],'Highest urgency')
        self.assertEqual(classify(detections[3],self.report)[0],'Low risk')
        self.assertEqual(match(self.db,'VID-001',SAMPLE,{'id':2}),3)
        self.assertEqual(match(self.db,'VID-001',SAMPLE,{'id':2}),0)
        self.assertEqual({x['urgency'] for x in self.db.execute('SELECT urgency FROM alerts')},{'High risk','Highest urgency','Low risk'})
    def test_plate_registered_to_alto_is_high_risk(self):
        detection=json.loads(json.dumps(SAMPLE['detections'][0]))
        detection['registration_comparison']['registered_vehicle']={'manufacturer':'Maruti','model':'Alto K10','colour':'white'}
        self.assertEqual(classify(detection,self.report)[0],'High risk')
    def test_missing_lookup_is_not_low_risk(self):
        detection=json.loads(json.dumps(SAMPLE['detections'][3]))
        detection['registration_comparison']['lookup_status']='failed'
        self.assertEqual(classify(detection,self.report)[0],'Needs lookup')
    def test_inactive_report_creates_no_alert(self):
        self.db.execute("UPDATE reports SET status='Recovered',monitoring=0")
        self.assertEqual(match(self.db,'VID-001',SAMPLE,{'id':2}),0)
    def test_registry_overrides_precomputed_lookup(self):
        self.assertEqual(match(self.db,'VID-001',SAMPLE,{'id':2}),3)
        self.db.execute("INSERT INTO rto_registry VALUES('JK02EF4321','Maruti','Alto K10','white','now')")
        self.assertEqual(match(self.db,'VID-001',SAMPLE,{'id':2}),0)
        alert=self.db.execute("SELECT urgency,lookup_source FROM alerts WHERE detection_id='DET-004'").fetchone()
        self.assertEqual((alert['urgency'],alert['lookup_source']),('High risk','Synthetic RTO registry'))
        missing=self.db.execute("SELECT urgency FROM alerts WHERE detection_id='DET-001'").fetchone()
        self.assertEqual(missing['urgency'],'Needs lookup')
    def test_csv_import_parser(self):
        records=parse_rto_import({'format':'csv','text':'plate,manufacturer,model,colour\nJK02 AB 1234,Mahindra,XUV700,black\n'})
        self.assertEqual(records,[('JK02AB1234','Mahindra','XUV700','black')])
    def test_existing_database_is_migrated(self):
        original_db,original_media=server.DB,server.MEDIA
        with tempfile.TemporaryDirectory() as folder:
            try:
                server.DB=Path(folder)/'old.db'; server.MEDIA=Path(folder)/'media'
                old=sqlite3.connect(server.DB)
                old.execute('CREATE TABLE alerts(id INTEGER PRIMARY KEY,video_id TEXT,detection_id TEXT,report_id INTEGER,timestamp REAL,plate TEXT,vehicle TEXT,similarity REAL,status TEXT,note TEXT,reviewer_id INTEGER,created_at TEXT,UNIQUE(video_id,detection_id,report_id))')
                old.commit(); old.close()
                server.init()
                check=sqlite3.connect(server.DB)
                columns={x[1] for x in check.execute('PRAGMA table_info(alerts)')}
                check.close()
                self.assertTrue({'urgency','reason','lookup_status','registered_vehicle','lookup_source'}<=columns)
            finally:
                server.DB,server.MEDIA=original_db,original_media

if __name__=='__main__': unittest.main()
