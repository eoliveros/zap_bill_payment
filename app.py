#!/usr/bin/python3
import os
import logging
import sys
import json
import time
import decimal
import hmac
import hashlib
import base64

from flask import url_for, redirect, render_template, request, abort, jsonify
from flask_security.utils import encrypt_password
from flask_socketio import Namespace, emit, join_room, leave_room
from flask_security import current_user
import werkzeug
import requests

from app_core import app, db, socketio
from models import security, user_datastore, Role, User, Invoice, Utility
import admin
from utils import check_hmac_auth, generate_key

logger = logging.getLogger(__name__)
ws_invoices = {}
ws_sids = {}
MAX_DETAIL_CHARS = 12

#
# Helper functions
#

def setup_logging(level):
    # setup logging
    logger.setLevel(level)
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter("[%(name)s %(levelname)s] %(message)s"))
    logger.addHandler(ch)
    # clear loggers set by any imported modules
    logging.getLogger().handlers.clear()

def add_user(email, password):
    with app.app_context():
        user = User.from_email(db.session, email)
        if user:
            #logger.error("user already exists")
            #return
            user.password = encrypt_password(password)
        else:
            user = user_datastore.create_user(email=email, password=encrypt_password(password))
        db.session.commit()

def create_role(name, desc):
    role = Role.from_name(db.session, name)
    if not role:
        role = Role(name=name, description=desc)
    else:
        role.description = desc
    db.session.add(role)
    return role

def add_role(email, role_name):
    with app.app_context():
        user = User.from_email(db.session, email)
        if not user:
            logger.error("user does not exist")
            return
        role = create_role(role_name, None)
        if role not in user.roles:
            user.roles.append(role)
        else:
            logger.info("user already has role")
        db.session.commit()

def check_auth(token, nonce, sig, body):
    invoice = Invoice.from_token(db.session, token)
    if not invoice:
        return False, "not found", None
    res, reason = check_hmac_auth(invoice, nonce, sig, body)
    if not res:
        return False, reason, None
    # update invoice nonce
    db.session.commit()
    return True, "", invoice

def bad_request(message):
    response = jsonify({"message": message})
    response.status_code = 400
    return response

#
# Flask views
#

@app.before_request
def before_request_func():
    if "DEBUG_REQUESTS" in app.config:
        print("URL: %s" % request.url)
        print(request.headers)

@app.route("/")
def index():
    return render_template("index.html")

#
# Test
#

@app.route("/test/invoice/<token>")
def test_invoice(token):
    if not app.config["DEBUG"]:
        return abort(404)
    invoice = Invoice.from_token(db.session, token)
    if token in ws_invoices:
        print("sending invoice update %s" % token)
        socketio.emit("info", invoice.to_json(), json=True, room=token)
    if invoice:
        return jsonify(invoice.to_json())
    return abort(404)

@app.route("/test/ws")
def test_ws():
    if not app.config["DEBUG"]:
        return abort(404)
    return jsonify(ws_invoices)

#
# Websocket events
#

def alert_invoice_update(invoice):
    socketio.emit("update", invoice.to_json(), json=True, room=invoice.token)

class SocketIoNamespace(Namespace):
    def trigger_event(self, event, sid, *args):
        if sid not in self.server.environ:
            # we don't have record of this client, ignore this event
            return '', 400
        app = self.server.environ[sid]['flask.app']
        if "DEBUG_REQUESTS" in app.config:
            with app.request_context(self.server.environ[sid]):
                before_request_func()
        return super(SocketIoNamespace, self).trigger_event(event, sid, *args)

    def on_error(self, e):
        print(e)

    def on_connect(self):
        print("connect sid: %s" % request.sid)

    def on_auth(self, auth):
        # check auth
        res, reason, invoice = check_auth(auth["token"], auth["nonce"], auth["signature"], str(auth["nonce"]))
        if res:
            emit("info", "authenticated!")
            # join room and store user
            print("join room for invoice: %s" % auth["token"])
            join_room(auth["token"])
            ws_invoices[auth["token"]] = request.sid
            ws_sids[request.sid] = auth["token"]

    def on_disconnect(self):
        print("disconnect sid: %s" % request.sid)
        if request.sid in ws_sids:
            token = ws_sids[request.sid]
            if token in ws_invoices:
                print("leave room for invoice: %s" % token)
                leave_room(token)
                del ws_invoices[token]
            del ws_sids[request.sid]

socketio.on_namespace(SocketIoNamespace("/"))

#
# Public endpoints
#

@app.route("/utilities")
def utilities():
    utilities = Utility.all_alphabetical(db.session)
    return render_template("utilities.html", utilities=utilities)

def validate_values(fields, values):
    for field in fields:
        name = field["label"]
        value = values[name]
        if not value and (not "allow_empty" in field or not field["allow_empty"]):
            return "please enter a value for '%s'" % name
        type_ = field["type"].lower()
        if type_ == "number":
            num = int(value)
            if "min" in field and num < field["min"]:
                return "value for '%s' has a minimum of %d" % (name, field["min"])
            if "max" in field and num > field["max"]:
                return "value for '%s' has a maximum of %d" % (name, field["max"])
        if type_ == "string":
            if "min_chars" in field and len(value) < field["min_chars"]:
                return "value for '%s' has a minimum number of characters of %d" % (name, field["min_chars"])
        max_chars = MAX_DETAIL_CHARS
        if isinstance(field["target"], list):
            max_chars = MAX_DETAIL_CHARS * len(field["target"])
        if len(value) > max_chars:
            return "value for '%s' is too long" % name
    return None

def bank_transaction_details(utility, values):
    details = {}
    for field in utility.fields_description_json:
        target = field["target"]
        value = values[field["label"]]
        if isinstance(target, list):
            for t in target:
                details[t], value = value[:MAX_DETAIL_CHARS], value[MAX_DETAIL_CHARS:]
        else:
            details[target] = value
    return details

def hmac_sha256(secret, msg):
    sig = hmac.new(bytes(secret, 'latin-1'), msg=bytes(msg, 'latin-1'), digestmod=hashlib.sha256).digest()
    return base64.b64encode(sig)

def create_invoice(utility, details, amount):
    # init bank recipient params
    reference = details["reference"] if "reference" in details else ""
    code = details["code"] if "code" in details else ""
    particulars = details["particulars"] if "particulars" in details else ""
    # create request body
    recipient_params = dict(reference=reference, code=code, particulars=particulars)
    body = dict(key=app.config["BRONZE_API_KEY"], nonce=int(time.time()), market="ZAPNZD", side="sell", amount=str(amount), amountasquotecurrency=True, recipient=utility.bank_account, customrecipientparams=recipient_params)
    json_body = json.dumps(body)
    # create hmac sha256 signature of body
    signature = hmac_sha256(app.config["BRONZE_API_SECRET"], json_body)
    # create request
    url = app.config["BRONZE_ADDRESS"] + "/api/v1/BrokerCreate"
    headers = {"Content-Type": "application/json", "X-Signature": signature}
    print(":: requesting %s.." % url)
    r = requests.post(url, headers=headers, data=json_body)
    try:
        r.raise_for_status()
    except:
        print("ERROR: response http status %d (%s)" % (r.status_code, r.content))
        return None
    # extract token and create invoice
    body = r.json()
    print(body)
    broker_token = body["token"]
    amount_cents = int(decimal.Decimal(body["amountSend"]) * 100)
    amount_cents_nzd = int(decimal.Decimal(body["amountReceive"]) * 100)
    invoice = Invoice(amount_cents, amount_cents_nzd, broker_token)
    db.session.add(invoice)
    db.session.commit()
    return invoice

@app.route("/utility", methods=["GET", "POST"])
def utility():
    STATE_CREATE = "create"
    STATE_CHECK = "check"

    utility_id = int(request.args.get("utility"))
    utility = Utility.from_id(db.session, utility_id)
    Utility.jsonify_fields_descriptions([utility])
    if request.method == "POST":
        state = request.form.get("zbp_state")
        amount = request.form.get("zbp_amount")
        values = request.form
        error = None
        if state == STATE_CREATE:
            try:
                # check amount
                amount = decimal.Decimal(amount)
                if amount <= 0:
                    error = "amount must be greater then zero"
                else:
                    # check field values
                    error = validate_values(utility.fields_description_json, values)
            except:
                error = "amount must be valid number"
            if not error:
                state = STATE_CHECK
        elif state == STATE_CHECK:
            details = bank_transaction_details(utility, values)
            invoice = create_invoice(utility, details, amount)
            if invoice:
                return redirect(url_for("invoice", token=invoice.token))
            else:
                error = "failed to create invoice"
        return render_template("utility.html", utility=utility, state=state, amount=amount, values=values, error=error)
    else:
        return render_template("utility.html", utility=utility, state=STATE_CREATE, values=werkzeug.MultiDict())

@app.route("/invoice")
def invoice():
    token = request.args.get("token")
    invoice = Invoice.from_token(db.session, token)
    if not invoice:
        return abort(404)
    return render_template("invoice.html", invoice=invoice)

if __name__ == "__main__":
    setup_logging(logging.DEBUG)

    # create tables
    db.create_all()
    create_role("admin", "super user")
    db.session.commit()

    # process commands
    if len(sys.argv) > 1:
        if sys.argv[1] == "add_user":
            add_user(sys.argv[2], sys.argv[3])
        if sys.argv[1] == "add_role":
            add_role(sys.argv[2], sys.argv[3])
    else:
        # check config
        if "BRONZE_API_KEY" not in app.config:
            logger.error("BRONZE_API_KEY does not exist")
            sys.exit(1)
        if "BRONZE_API_SECRET" not in app.config:
            logger.error("BRONZE_API_SECRET does not exist")
            sys.exit(1)

        # Bind to PORT if defined, otherwise default to 5000.
        port = int(os.environ.get("PORT", 5000))
        print("binding to port: %d" % port)
        socketio.run(app, host="0.0.0.0", port=port)
