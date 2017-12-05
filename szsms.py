# pylint: disable=C0103,R0912,R0915,C0301,R0914,R0902
# pylint: disable=no-member
"""service to send sms via dinstar gsm gate service
stub from gsminform.ru"""
import time
import json
import os
from tempfile import gettempdir
import paramiko
from flask import Flask, request

app = Flask(__name__)
api_key = 'crv1PY4s1wc3AQNtKn0Byl1n'
gate_addr = '192.168.22.199'
gate_path = '/var/spool/dwgp/send/'
gate_user = 'root'
gate_pass = 'jU3hd9Hl'

def send_sms(sms_id, sms_phone, sms_msg):
    """send sms via dinstar gate"""
    fn = '{}.txt'.format(sms_id)
    tmp_fn = os.path.join(gettempdir(), fn)
    with open(tmp_fn, 'w', encoding='utf-8', errors='ignore') as tmp_f:
        tmp_f.write(sms_phone + '\n')
        tmp_f.write('0\n')
        tmp_f.write(sms_msg+'\n')
    transport = paramiko.Transport((gate_addr, 22))
    transport.connect(username=gate_user, password=gate_pass)
    sftp = paramiko.SFTPClient.from_transport(transport)
    sftp.put(tmp_fn, gate_path + fn)
    sftp.close()
    os.remove(tmp_fn)

def check_sms(sms_id):
    """check sms state - supported sent and sending states"""
    fn = '{}.txt'.format(sms_id)
    transport = paramiko.Transport((gate_addr, 22))
    transport.connect(username=gate_user, password=gate_pass)
    sftp = paramiko.SFTPClient.from_transport(transport)
    status = False
    try:
        sftp.stat(gate_path + fn)
    except FileNotFoundError:
        status = True
    return status

@app.route('/api/', methods=['POST', 'GET'])
def sms_api():
    """handle send and status cmd
    status always return OK"""
    if request.method == 'POST':
        rq_cmd = request.form['cmd']
        rq_apikey = request.form['api_key']
        if rq_apikey != api_key:
            return json.dumps({
                'error_no':1,
                'error_msg':'API Key Not Found'
            })
        if rq_cmd == 'send':
            rq_msg = request.form['message']
            rq_to = request.form['to']
            rq_tm = str(int(time.time()))
            send_sms(rq_tm, rq_to, rq_msg)
            return json.dumps({
                'error_no':0,
                'error_msg':'OK',
                'items':[{
                    'phone':rq_to,
                    'sms_id':rq_tm,
                    'error_no':0,
                    'error_msg':'OK'
                }]
            })
        if rq_cmd == 'status':
            sms_id = request.form['sms_id']
            if check_sms(sms_id):
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8787)
