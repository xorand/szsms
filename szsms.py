# pylint: disable=C0103,R0912,R0915,C0301,R0914,R0902,R1702,W0702
# pylint: disable=no-member
"""service to send sms via dinstar gsm gate service
stub from gsminform.ru"""
import uuid
import json
import socket
import select
from datetime import datetime
import threading
from threading import Timer
import logging
from logging.handlers import RotatingFileHandler
import configparser
import sqlite3
from struct import pack, unpack
from random import randint
from time import time
from flask import Flask, request, redirect

# flask app var
app = Flask(__name__)
# global cfg var
cfg = {}
# send queue vars
sq_lock = threading.RLock()
sq = []

# constants
# msg types
SMS_IN = 0
SMS_OUT = 1
USSD_IN = 2
USSD_OUT = 3
# status
S_SENDING = 0
S_SENT = 1

def read_cfg():
    """read config"""
    cfn = __file__.replace('.py', '.ini')
    cfh = 'szsms'
    config = configparser.ConfigParser()
    config.read(cfn, encoding='utf-8')
    logger = logging.getLogger()
    logger.setLevel(config.getint(cfh, 'log_level'))
    handler = RotatingFileHandler(
        __file__.replace('.py', '.log'),
        maxBytes=config.getint(cfh, 'log_size'),
        backupCount=config.getint(cfh, 'log_num'))
    formatter = logging.Formatter('%(asctime)-15s %(levelname)-7.7s %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    cfg['api_key'] = config.get(cfh, 'api_key')
    cfg['api_host'] = config.get(cfh, 'api_host')
    cfg['api_port'] = config.getint(cfh, 'api_port')
    cfg['gw_addr'] = config.get(cfh, 'gw_addr')
    cfg['gw_port'] = config.getint(cfh, 'gw_port')
    cfg['gw_ping_timer'] = config.getint(cfh, 'gw_ping_timer')
    cfg['gw_queue_timer'] = config.getint(cfh, 'gw_queue_timer')
    # init db conn
    cfg['dbfn'] = __file__.replace('.py', '.sqlite')
    dbconn = sqlite3.connect(cfg['dbfn'])
    cursor = dbconn.cursor()
    cursor.execute("""CREATE TABLE IF NOT EXISTS msg(
        phone TEXT,
        msg TEXT,
        msg_id TEXT,
        msg_date TXT,
        msg_type INT,
        slot INT,
        status INT
        )""")
    cursor.execute('CREATE INDEX IF NOT EXISTS msg_id ON msg (msg_id)')
    dbconn.commit()
    dbconn.close()

def gw_send(header, sdata):
    """send data to gw"""
    pkt = pack('!L', len(sdata['body']))
    pkt += pack('!6s', header['id']['mac']) + b'\x00\x00'
    pkt += pack('!L', header['id']['time'])
    pkt += pack('!L', header['id']['serial'])
    pkt += pack('!H', sdata['type'])
    pkt += pack('!H', 0)
    pkt += sdata['body']
    with sq_lock:
        sq.append(pkt)

def gw_create_header():
    """create header for gw"""
    return {'id': {'mac': b'\x00\xfa\xb3\xd2\xd3\xaa',
                   'time': int(time()),
                   'serial': randint(1, 1000000)}}

def gw_parse_data(data):
    """parse data from gw"""
    if data:
        logging.info('<- %s', data.hex())
        while data:
            header = {'len': unpack('!L', data[0:4])[0],
                      'id': {'mac': unpack('!6s', data[4:10])[0],
                             'time': unpack('!L', data[12:16])[0],
                             'serial': unpack('!L', data[16:20])[0]},
                      'type': unpack('!H', data[20:22])[0],
                      'flag': unpack('!H', data[22:24])[0]}
            data_len = 24 + header['len']
            if len(data) < data_len:
                break
            sdata = gw_parse_type(header['type'], data[24:data_len])
            if sdata['type']:
                gw_send(header, sdata)
            data = data[data_len:]

def gw_save_sms(body):
    """saving sms to database"""
    if body['encoding'] == 0:
        msg = body['content'].decode("utf-8")
    elif body['encoding'] == 1:
        msg = body['content'].decode('utf-16-be')
    try:
        msg_date = datetime.strptime(body['timestamp'].decode(), '%Y%m%d%H%M%S')
    except ValueError:
        msg_date = datetime.now()
    dbconn = sqlite3.connect(cfg['dbfn'])
    cursor = dbconn.cursor()
    msg_id = uuid.uuid4().hex
    cursor.execute('INSERT INTO msg(phone, msg, msg_id, msg_date, msg_type, slot, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                   (body['number'].decode("utf-8"), msg, msg_id, msg_date, SMS_IN, body['port'] + 1, S_SENT))
    dbconn.commit()
    dbconn.close()
    logging.info('<- sms from number %s', body['number'])

def gw_save_ussd(body):
    """saving ussd to database"""
    if body['encoding'] == 0:
        msg = ''.join([chr(int(body['content'][pos:pos+4], 16)) for pos in range(0, len(body['content']), 4)])
    elif body['encoding'] == 1:
        msg = body['content'].decode('utf-16-be')
    msg_date = datetime.now()
    dbconn = sqlite3.connect(cfg['dbfn'])
    cursor = dbconn.cursor()
    msg_id = uuid.uuid4().hex
    cursor.execute('INSERT INTO msg(phone, msg, msg_id, msg_date, msg_type, slot, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                   ('', msg, msg_id, msg_date, USSD_IN, body['port'] + 1, S_SENT))
    dbconn.commit()
    dbconn.close()
    logging.info('<- ussd from port %s', body['port'])

def gw_parse_type(htype, data):
    """parsing gw types"""
    sdata = {'type': 0,
             'body': b''}
    if htype == 0:   # ping alive
        cfg['ping_sent'] = False
        logging.info('<- gw alive')
    elif htype == 7:   # status message
        logging.info('<- status message')
        sdata['type'] = 8
        sdata['body'] = pack('!?', False)
    elif htype == 5:   # receive message
        logging.info('<- receive message')
        body = {'number': unpack('!24s', data[0:24])[0].replace(b'\x00', b''),
                'type': unpack('!B', bytes([data[24]]))[0],
                'port': unpack('!B', bytes([data[25]]))[0],
                'timestamp': unpack('!15s', data[26:41])[0].replace(b'\x00', b''),
                'timezone': unpack('!b', bytes([data[41]]))[0],
                'encoding': unpack('!B', bytes([data[42]]))[0],
                'length': unpack('!H', bytes(data[43:45]))[0]}
        body['content'] = unpack('!%ds' % body['length'], bytes(data[45:]))[0]
        gw_save_sms(body)
        sdata['type'] = 6
        sdata['body'] = pack('!?', False)
    elif htype == 3:  # sms result
        logging.info('<- sms result')
        sdata['type'] = 4
        sdata['body'] = pack('!?', False)
    elif htype == 11:  # ussd result
        logging.info('<- ussd result')
        body = {'port': unpack('!B', bytes([data[0]]))[0],
                'status': unpack('!B', bytes([data[1]]))[0],
                'length': unpack('!H', bytes(data[2:4]))[0],
                'encoding': unpack('!B', bytes([data[4]]))[0]}
        body['content'] = unpack('!%ds' % body['length'], bytes(data[5:]))[0]
        gw_save_ussd(body)
        sdata['type'] = 12
        sdata['body'] = pack('!?', False)
    elif htype == 515:  # call state report
        logging.info('<- call state result')
        sdata['type'] = 516
        sdata['body'] = pack('!?', False)
    return sdata

def gw_ping_fn():
    """ping gateway function"""
    if cfg['ping_sent']:
        cfg['gw_alive'] = False
    sdata = {'type': 0,
             'body': b''}
    gw_send(gw_create_header(), sdata)
    cfg['ping_tm'] = Timer(cfg['gw_ping_timer'], gw_ping_fn)
    cfg['ping_tm'].start()
    cfg['ping_sent'] = True

def gw_queue_fn():
    """proceed sms/ussd queue to gw"""
    dbconn = sqlite3.connect(cfg['dbfn'])
    cursor = dbconn.cursor()
    for row in cursor.execute('SELECT phone, msg, msg_id, msg_type, slot FROM msg WHERE status = ?', (S_SENDING,)):
        phone = row[0]
        msg = row[1]
        msg_id = row[2]
        msg_type = row[3]
        slot = row[4]
        if msg_type == SMS_OUT:
            number = str(phone.strip())
            port = slot - 1
            content = b''.join([pack('!H', ord(l)) for l in msg])
            sdata = {'type': 1,
                     'body': pack('!BBBB24sH%ds' % len(content), port, 1, 0, 1, number.encode(), len(content), content)}
            gw_send(gw_create_header(), sdata)
            logging.info('sending sms to number %s', number)
        if msg_type == USSD_OUT:
            port = slot - 1
            number = msg.strip()
            sdata = {'type': 9,
                     'body': pack('!BBH%ds' % len(number), port, 1, len(number), number.encode())}
            gw_send(gw_create_header(), sdata)
            logging.info('sending ussd to port %s', port)
        cursor_id = dbconn.cursor()
        cursor_id.execute('UPDATE msg SET status = ? WHERE msg_id = ?', (S_SENT, msg_id))
        dbconn.commit()
    dbconn.close()
    cfg['queue_tm'] = Timer(cfg['gw_queue_timer'], gw_queue_fn)
    cfg['queue_tm'].start()

def gw_disc(s, inputs, outputs):
    """gw disconnect"""
    if s in inputs:
        inputs.remove(s)
    if s in outputs:
        outputs.remove(s)
    s.close()
    if not cfg['ping_tm'] is None:
        cfg['ping_tm'].cancel()
    if not cfg['queue_tm'] is None:
        cfg['queue_tm'].cancel()
    cfg['gw_alive'] = False
    logging.info('gateway disconnected')

def gw_th_fn():
    """thread function to work with dinstar gateway"""
    while True:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(('', cfg['gw_port']))
        server.listen(1)
        logging.info('server listening')
        # timers
        cfg['ping_tm'] = None
        cfg['queue_tm'] = None
        cfg['ping_sent'] = False
        cfg['gw_alive'] = False
        #
        inputs = [server]
        outputs = []
        while inputs:
            readable, writable, exceptional = select.select(inputs, outputs, inputs)
            for s in readable:
                if s is server:
                    sconn, saddr = s.accept()
                    sconn.setblocking(0)
                    # cleanup inputs/outputs
                    for sock in inputs:
                        if sock is not server:
                            sock.close()
                            logging.info('gateway reconnected')
                    inputs.clear()
                    outputs.clear()
                    if not cfg['ping_tm'] is None:
                        cfg['ping_tm'].cancel()
                    if not cfg['queue_tm'] is None:
                        cfg['queue_tm'].cancel()
                    cfg['ping_sent'] = False
                    cfg['gw_alive'] = True
                    # go on
                    inputs.append(server)
                    inputs.append(sconn)
                    outputs.append(sconn)
                    logging.info('gateway connected %s', saddr[0])
                    Timer(cfg['gw_ping_timer'], gw_ping_fn).start()
                    Timer(cfg['gw_queue_timer'], gw_queue_fn).start()
                else:
                    if cfg['gw_alive']:
                        try:
                            data = s.recv(66560)
                            if data:
                                gw_parse_data(data)
                            else:
                                gw_disc(s, inputs, outputs)
                        except:
                            gw_disc(s, inputs, outputs)
                    else:
                        gw_disc(s, inputs, outputs)
            for s in writable:
                if s is not server:
                    if cfg['gw_alive']:
                        with sq_lock:
                            for pkt in sq:
                                logging.info('-> %s', pkt.hex())
                                s.send(pkt)
                            sq.clear()
                    else:
                        gw_disc(s, inputs, outputs)
            for s in exceptional:
                gw_disc(s, inputs, outputs)
        server.close()
        logging.info('server restarted')

def add_msg(msg_type, phone, msg, slot):
    """add msg to msg queue"""
    dbconn = sqlite3.connect(cfg['dbfn'])
    cursor = dbconn.cursor()
    msg_id = uuid.uuid4().hex
    cursor.execute('INSERT INTO msg(phone, msg, msg_id, msg_date, msg_type, slot, status) VALUES (?, ?, ?, ?, ?, ?, ?)',
                   (phone, msg, msg_id, datetime.now(), msg_type, slot, S_SENDING))
    dbconn.commit()
    dbconn.close()
    return msg_id

def api_send_sms(sms_phone, sms_msg):
    """api send sms function"""
    return add_msg(SMS_OUT, sms_phone, sms_msg, 1)

def api_check_sms(sms_id):
    """api check sms state true means send false meeans sending"""
    dbconn = sqlite3.connect(cfg['dbfn'])
    cursor = dbconn.cursor()
    status = False
    for row in cursor.execute('SELECT status FROM msg WHERE msg_id = ?', (sms_id,)):
        if row[0] == S_SENT:
            status = True
    dbconn.close()
    return status

@app.route('/api/', methods=['POST', 'GET'])
def api():
    """handle send and status cmd
    status always return OK"""
    if request.method == 'POST':
        rq_cmd = request.form['cmd']
        rq_apikey = request.form['api_key']
        if rq_apikey != cfg['api_key']:
            return json.dumps({
                'error_no':1,
                'error_msg':'API Key Not Found'
            })
        if rq_cmd == 'send':
            rq_msg = request.form['message']
            rq_to = request.form['to']
            sms_id = api_send_sms(rq_to, rq_msg)
            return json.dumps({
                'error_no':0,
                'error_msg':'OK',
                'items':[{
                    'phone':rq_to,
                    'sms_id':sms_id,
                    'error_no':0,
                    'error_msg':'OK'
                }]
            })
        if rq_cmd == 'status':
            sms_id = request.form['sms_id']
            if api_check_sms(sms_id):
                status = json.dumps({
                    'error_no':0,
                    'error_msg':'OK',
                    'items':[{
                        'status_no':'2',
                        'error_msg':'OK'
                    }]
                })
            else:
                status = json.dumps({
                    'error_no':0,
                    'error_msg':'OK',
                    'items':[{
                        'status_no':'10',
                        'error_msg':'OK'
                    }]
                })
            return status
    else:
        return json.dumps({
            'error_no':0,
            'error_msg':'OK'
        })

@app.route('/send_sms/', methods=['POST', 'GET'])
def www_send_sms():
    """send sms page"""
    tpl = """
    <html>
    <form action="" method="post">
        <table>
        <tr><td>phone</td><td><input name="phone" type="text" size="10"></td></tr>
        <tr><td>sms text</td><td><textarea name="sms" cols="40" rows="3"></textarea></td></tr>
        <tr><td></td><td><input type="radio" name="slot" value="1" checked>slot #1</td></tr>
        <tr><td></td><td><input type="radio" name="slot" value="2">slot #2</td></tr>
        <tr><td></td><td><input type="submit" value="send"/></td></tr>
    </form>
    </html>
    """
    html = ''
    if request.method == 'POST':
        add_msg(
            SMS_OUT,
            request.form['phone'],
            request.form['sms'],
            int(request.form['slot'])
            )
        html = redirect('/')
    else:
        html = tpl
    return html

@app.route('/send_ussd/', methods=['POST', 'GET'])
def www_send_ussd():
    """send ussd page"""
    tpl = """
    <html>
    <form action="" method="post">
        <table>
        <tr><td>ussd</td><td><input name="ussd" type="text" size="10"></td></tr>
        <tr><td></td><td><input type="radio" name="slot" value="1" checked>slot #1</td></tr>
        <tr><td></td><td><input type="radio" name="slot" value="2">slot #2</td></tr>
        <tr><td></td><td><input type="submit" value="send"/></td></tr>
    </form>
    </html>
    """
    html = ''
    if request.method == 'POST':
        add_msg(
            USSD_OUT,
            '',
            request.form['ussd'],
            int(request.form['slot'])
            )
        html = redirect('/')
    else:
        html = tpl
    return html

@app.route('/', methods=['POST', 'GET'])
def www_root():
    """www root page"""
    tpl = """
    <html>
    gw status:{}<br>
    <a href="/send_sms/">send sms</a><br>
    <a href="/send_ussd/">send ussd</a><br>
    <a href="/base/">msg base</a><br>
    </form>
    </html>
    """
    if cfg['gw_alive']:
        status = '<font color="green">alive</font>'
    else:
        status = '<font color="red">dead</font>'
    return tpl.format(status)

@app.route('/base/', methods=['POST', 'GET'])
def www_base():
    """www base page"""
    html = """
    <html>
    <table border=1>
    <th>date</th><th>msg type</th><th>phone</th><th>msg id</th><th>msg</th><th>slot</th><th>status</th>
    """
    dbconn = sqlite3.connect(cfg['dbfn'])
    cursor = dbconn.cursor()
    for row in cursor.execute('SELECT phone, msg, msg_id, strftime("%d.%m.%Y %H:%M:%S", msg_date), msg_type, slot, status FROM msg'):
        phone = row[0]
        msg = row[1]
        msg_id = row[2]
        msg_date = row[3]
        if row[4] == SMS_IN:
            msg_type = 'sms in'
        if row[4] == SMS_OUT:
            msg_type = 'sms out'
        if row[4] == USSD_IN:
            msg_type = 'ussd in'
        if row[4] == USSD_OUT:
            msg_type = 'ussd out'
        slot = row[5]
        if row[6] == S_SENDING:
            status = 'sending'
        if row[6] == S_SENT:
            status = 'sent'
        html = html + '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(
            msg_date, msg_type, phone, msg_id, msg, slot, status)
    dbconn.close()
    html = html + '</table><html>'
    return html

if __name__ == '__main__':
    read_cfg()
    cfg['th_gw'] = threading.Thread(target=gw_th_fn, args=())
    cfg['th_gw'].start()
    app.run(host=cfg['api_host'], port=cfg['api_port'])
