"""
Qt (PySide6) based Live-Watch-like GUI for the JSON object protocol.

Features:
- List available serial ports, set baudrate
- Connect / Disconnect
- Input text box: type an object name (e.g., laser) and press Discover
- Shows schema and latest state in a QTreeWidget
- Persists last used port, baud, and discovered objects to gui_config.json

Run: pip install -r py_scripts/requirements.txt
       python py_scripts/gui.py

"""
import sys
import json
import threading
from queue import Queue, Empty
import time
from pathlib import Path

from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                               QPushButton, QComboBox, QLabel, QLineEdit, QTextEdit,
                               QTreeWidget, QTreeWidgetItem, QMessageBox, QCheckBox,
                               QTableWidget, QTableWidgetItem, QInputDialog, QAbstractItemView, QSplitter)
from PySide6.QtCore import Qt, QTimer, QEvent
from PySide6.QtGui import QColor

import serial
import serial.tools.list_ports

CONFIG_PATH = Path(__file__).parent / "gui_config.json"

class LiveWatchGUI(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('LiveWatch - Object Inspector')
        self.resize(800, 600)

        self.ser = None
        self.reader_thread = None
        self.reader_running = False

        self.cache = {
            'schemas': {},
            'states': {}
        }
        # count of previously saved expressions (from config) shown in placeholder
        self.past_expr_count = 0
        # map of object name -> expiry timestamp for recently requested info
        self.expecting = {}
        # open dialogs for object drill-down: name -> {dialog, table, update}
        self.open_object_dialogs = {}
        # track which expressions are expanded inline
        self.expanded_exprs = set()
        # subscription handling: only accept unsolicited updates for subscribed objects
        self.subscriptions = set()
        self.require_subscription = True
        # requests to send once serial connection established (list of (type, name))
        self._pending_startup_requests = []
        # queue for lines read from serial (processed in GUI thread)
        self._incoming_queue = Queue()
        # timer to process incoming serial lines on the GUI thread
        self._process_timer = QTimer(self)
        self._process_timer.setInterval(25)
        self._process_timer.timeout.connect(self._process_incoming)
        self._process_timer.start()

        self._build_ui()
        self._load_config()
        self._populate_ports()

        # timer to refresh port list
        self.port_timer = QTimer(self)
        self.port_timer.setInterval(2000)
        self.port_timer.timeout.connect(self._populate_ports)
        self.port_timer.start()

    def _build_ui(self):
        # Use a splitter so user can resize top (controls + table) and bottom (log)
        main_layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Vertical)

        # top widget contains controls and the expressions table
        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)

        # top: port selection
        h = QHBoxLayout()
        self.port_combo = QComboBox()
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["9600", "19200", "38400", "57600", "115200"])
        self.connect_btn = QPushButton('Connect')
        self.connect_btn.clicked.connect(self.toggle_connect)
        h.addWidget(QLabel('Port:'))
        h.addWidget(self.port_combo)
        h.addWidget(QLabel('Baud:'))
        h.addWidget(self.baud_combo)
        h.addWidget(self.connect_btn)
        top_layout.addLayout(h)

        # expressions table (Glyph | Expression | Type | Value)
        self.expr_table = QTableWidget(0, 4)
        self.expr_table.setHorizontalHeaderLabels(['', 'Expression', 'Type', 'Value'])
        self.expr_table.verticalHeader().setVisible(False)
        self.expr_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # allow inline editing of the Expression column (Excel-like)
        self.expr_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked)
        # allow manual resizing of columns; make Value column stretch to fill space
        try:
            from PySide6.QtWidgets import QHeaderView
            # glyph col fixed, Expression and Type interactive, Value stretches
            self.expr_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
            self.expr_table.setColumnWidth(0, 24)
            self.expr_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
            self.expr_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Interactive)
            self.expr_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        except Exception:
            pass
        # make rows adjust their height to contents
        self.expr_table.setWordWrap(True)
        self.expr_table.resizeRowsToContents()
        # add placeholder row and wire handlers
        self._add_expr_placeholder()
        self.expr_table.cellDoubleClicked.connect(self.on_expr_double_clicked)
        # single-click on glyph column should toggle expand/collapse without editing the Expression cell
        self.expr_table.cellClicked.connect(self.on_expr_cell_clicked)
        self.expr_table.cellChanged.connect(self.on_expr_cell_changed)
        self.expr_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.expr_table.customContextMenuRequested.connect(self.on_expr_context_menu)
        # allow Delete key handling via event filter
        self.expr_table.installEventFilter(self)
        top_layout.addWidget(self.expr_table)

        # discover / subscribe input
        h2 = QHBoxLayout()
        self.object_input = QLineEdit()
        self.object_input.setPlaceholderText('Type object name (e.g., laser)')
        self.discover_btn = QPushButton('Get')
        self.discover_btn.clicked.connect(self.on_discover)
        self.add_btn = QPushButton('Add Object')
        self.add_btn.clicked.connect(self.on_add_object)
        self.subscribe_btn = QPushButton('Subscribe')
        self.subscribe_btn.clicked.connect(self.on_subscribe)
        self.unsubscribe_btn = QPushButton('Unsubscribe')
        self.unsubscribe_btn.clicked.connect(self.on_unsubscribe)
        self.require_sub_chk = QCheckBox('Require subscription for unsolicited updates')
        self.require_sub_chk.setChecked(True)
        h2.addWidget(self.object_input)
        h2.addWidget(self.discover_btn)
        h2.addWidget(self.add_btn)
        h2.addWidget(self.subscribe_btn)
        h2.addWidget(self.unsubscribe_btn)
        h2.addWidget(self.require_sub_chk)
        top_layout.addLayout(h2)

        splitter.addWidget(top_widget)

    # (filter removed per user request)

        # lower: raw log (we no longer show the object/field tree)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        splitter.addWidget(self.log)
        # style the splitter handle to be easier to grab
        try:
            splitter.setHandleWidth(8)
            splitter.setStyleSheet("QSplitter::handle { background: #444; } QSplitter::handle:hover { background: #666; }")
        except Exception:
            pass

        main_layout.addWidget(splitter)
        self.setLayout(main_layout)

    def _log(self, *parts):
        s = ' '.join(str(p) for p in parts)
        self.log.append(s)

    def eventFilter(self, obj, event):
        # intercept Delete key on the expressions table to remove top-level expressions
        try:
            if obj is self.expr_table and event.type() == QEvent.KeyPress:
                from PySide6.QtGui import QKeyEvent
                key = event.key()
                if key == Qt.Key_Delete:
                    sel = self.expr_table.selectionModel().selectedRows()
                    if not sel:
                        return False
                    # map selected rows to top-level expression rows (if user selected a field row)
                    rows = []
                    for s in sel:
                        r0 = s.row()
                        try:
                            meta = self.expr_table.item(r0, 1).data(Qt.UserRole)
                        except Exception:
                            meta = None
                        if isinstance(meta, dict) and meta.get('isField'):
                            parent = meta.get('parent')
                            prow = self._find_expr_row(parent)
                            if prow is not None:
                                rows.append(prow)
                        else:
                            rows.append(r0)
                    # deduplicate and sort descending so row indices remain valid while removing
                    rows = sorted(set(rows), reverse=True)
                    for r in rows:
                        try:
                            self._remove_expression_row(r)
                        except Exception:
                            # fallback: attempt to remove row directly and unsubscribe
                            try:
                                it = self.expr_table.item(r, 1)
                                if it:
                                    name = it.text().replace('▶ ','').replace('▼ ','').strip()
                                    if name in self.subscriptions:
                                        try:
                                            self._send({'id': 'unsub-'+name, 'type': 'unsubscribe', 'path': name})
                                            try:
                                                self.subscriptions.remove(name)
                                            except Exception:
                                                pass
                                        except Exception:
                                            pass
                                self.expr_table.removeRow(r)
                            except Exception:
                                pass
                    # save config after removals
                    self._save_config()
                    return True
        except Exception:
            pass
        return super().eventFilter(obj, event)

    # --- expressions table helpers (manage rows and values) ---
    def _find_expr_row(self, expr_name):
        base = self.expr_table
        for r in range(base.rowCount()):
            it = base.item(r, 1)
            if not it:
                continue
            if it.text().strip() == expr_name:
                return r
        return None

    def _ensure_expr_row(self, expr_name):
        r = self._find_expr_row(expr_name)
        if r is not None:
            return r
        # insert before placeholder
        base = self.expr_table
        last = max(0, base.rowCount()-1)
        base.blockSignals(True)
        base.insertRow(last)
        # glyph blank
        base.setItem(last, 0, QTableWidgetItem(''))
        it = QTableWidgetItem(expr_name)
        it.setFlags(it.flags() | Qt.ItemIsEditable)
        it.setData(Qt.UserRole, expr_name)
        base.setItem(last, 1, it)
        base.setItem(last, 2, QTableWidgetItem(''))
        base.setItem(last, 3, QTableWidgetItem(''))
        base.blockSignals(False)
        return last

    def _remove_expression_row(self, row):
        """Remove a top-level expression row and any trailing expanded field rows.
        Unsubscribe if necessary, update subscriptions set, and save config.
        """
        base = self.expr_table
        if row < 0 or row >= base.rowCount():
            return
        # ensure this is not a field row
        it = base.item(row, 1)
        if it is None:
            return
        try:
            meta = it.data(Qt.UserRole)
            if isinstance(meta, dict) and meta.get('isField'):
                # nothing to remove here
                return
        except Exception:
            pass
        name = it.text().replace('▶ ', '').replace('▼ ', '').strip()
        # unsubscribe if subscribed
        if name in self.subscriptions:
            try:
                self._send({'id': 'unsub-'+name, 'type': 'unsubscribe', 'path': name})
                try:
                    self.subscriptions.remove(name)
                except Exception:
                    pass
            except Exception:
                pass
        # clear expanded state and close any open dialogs for this object
        try:
            if name in self.expanded_exprs:
                try:
                    self.expanded_exprs.remove(name)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if name in self.open_object_dialogs:
                try:
                    dlg = self.open_object_dialogs[name]['dialog']
                    dlg.close()
                except Exception:
                    pass
                try:
                    del self.open_object_dialogs[name]
                except Exception:
                    pass
        except Exception:
            pass
        # remove trailing field rows
        i = row + 1
        while i < base.rowCount():
            it2 = base.item(i, 1)
            if it2 is None:
                break
            try:
                meta2 = it2.data(Qt.UserRole)
                if isinstance(meta2, dict) and meta2.get('isField'):
                    base.removeRow(i)
                    continue
            except Exception:
                pass
            break
        # extra safety: sweep table and remove any dangling field rows for this parent
        try:
            r2 = 0
            while r2 < base.rowCount():
                try:
                    it3 = base.item(r2, 1)
                    if it3:
                        meta3 = it3.data(Qt.UserRole)
                        if isinstance(meta3, dict) and meta3.get('isField') and meta3.get('parent') == name:
                            base.removeRow(r2)
                            continue
                except Exception:
                    pass
                r2 += 1
        except Exception:
            pass
        # remove the parent row itself
        base.removeRow(row)
        self._save_config()
        self._log('Removed expression and unsubscribed', name)

    def _set_expr_value(self, expr_name, value):
        r = self._find_expr_row(expr_name)
        if r is None:
            r = self._ensure_expr_row(expr_name)
        try:
            self.expr_table.item(r, 3).setText(str(value))
        except Exception:
            self.expr_table.setItem(r, 3, QTableWidgetItem(str(value)))

    def _style_expr_row(self, row):
        try:
            item = self.expr_table.item(row, 1)
            if item:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
        except Exception:
            pass

    def _style_field_row(self, row):
        try:
            # indent field name (in Expression column) and apply light background
            item = self.expr_table.item(row, 1)
            if item:
                item.setText('  ' + item.text())
            # set background color for the row for readability
            for c in range(4):
                it = self.expr_table.item(row, c)
                if it:
                    it.setBackground(QColor('#2b2b2b'))
                    it.setForeground(QColor('#e6e6e6'))
        except Exception:
            pass

    def _refresh_expanded_expr(self, expr_name):
        # update field rows for an expanded expression from cache
        if expr_name not in self.expanded_exprs:
            return
        r = self._find_expr_row(expr_name)
        if r is None:
            return
        state = self.cache.get('states', {}).get(expr_name)
        base = self.expr_table
        base.blockSignals(True)
        # remove existing field rows first (they are marked by isField)
        i = r + 1
        while i < base.rowCount():
            it = base.item(i, 1)
            if it is None:
                break
            meta = it.data(Qt.UserRole)
            if isinstance(meta, dict) and meta.get('isField'):
                base.removeRow(i)
                continue
            break

        # re-insert rows from state
        insert_at = r + 1
        if state is None:
            # show placeholder row indicating no state yet
            base.insertRow(insert_at)
            base.setItem(insert_at, 0, QTableWidgetItem(''))
            fk = QTableWidgetItem('<no state>')
            fk.setData(Qt.UserRole, {'isField': True, 'parent': expr_name, 'field': '<no state>'})
            base.setItem(insert_at, 1, fk)
            base.setItem(insert_at, 2, QTableWidgetItem(''))
            base.setItem(insert_at, 3, QTableWidgetItem(''))
            self._style_field_row(insert_at)
        elif isinstance(state, dict):
            for k in sorted(state.keys()):
                v = state[k]
                base.insertRow(insert_at)
                # glyph blank for field row
                base.setItem(insert_at, 0, QTableWidgetItem(''))
                fk = QTableWidgetItem(str(k))
                fk.setData(Qt.UserRole, {'isField': True, 'parent': expr_name, 'field': k})
                base.setItem(insert_at, 1, fk)
                base.setItem(insert_at, 2, QTableWidgetItem(''))
                val_it = QTableWidgetItem(str(v))
                val_it.setFlags(val_it.flags() | Qt.ItemIsEditable)
                base.setItem(insert_at, 3, val_it)
                self._style_field_row(insert_at)
                insert_at += 1
        else:
            base.insertRow(insert_at)
            base.setItem(insert_at, 0, QTableWidgetItem(''))
            fk = QTableWidgetItem('<value>')
            fk.setData(Qt.UserRole, {'isField': True, 'parent': expr_name, 'field': '<value>'})
            base.setItem(insert_at, 1, fk)
            base.setItem(insert_at, 2, QTableWidgetItem(''))
            val_it = QTableWidgetItem(str(state))
            val_it.setFlags(val_it.flags() | Qt.ItemIsEditable)
            base.setItem(insert_at, 3, val_it)
            self._style_field_row(insert_at)
        base.blockSignals(False)

    def _update_expanded_fields_from_state(self, expr_name):
        """Update Value column (col 3) of already-expanded field rows in-place from cached state.
        If no field rows are present for the expanded expression, fall back to _refresh_expanded_expr to insert them.
        This avoids remove/insert churn when many updates arrive rapidly.
        """
        if expr_name not in self.expanded_exprs:
            return
        r = self._find_expr_row(expr_name)
        if r is None:
            return
        state = self.cache.get('states', {}).get(expr_name)
        base = self.expr_table
        base.blockSignals(True)
        i = r + 1
        found_field = False
        while i < base.rowCount():
            it = base.item(i, 1)
            if it is None:
                break
            try:
                meta = it.data(Qt.UserRole)
            except Exception:
                meta = None
            if isinstance(meta, dict) and meta.get('isField'):
                found_field = True
                field_name = meta.get('field')
                # derive new value from state
                if isinstance(state, dict):
                    newv = state.get(field_name, '')
                else:
                    # for scalar parent, only '<value>' field should update
                    if field_name == '<value>':
                        newv = state
                    else:
                        newv = ''
                try:
                    base.item(i, 3).setText(str(newv))
                except Exception:
                    base.setItem(i, 3, QTableWidgetItem(str(newv)))
                i += 1
                continue
            break
        base.blockSignals(False)
        if not found_field:
            # no inline field rows present — insert them
            QTimer.singleShot(0, lambda n=expr_name: self._refresh_expanded_expr(n))

    def _set_expr_type(self, expr_name, typ):
        r = self._find_expr_row(expr_name)
        if r is None:
            r = self._ensure_expr_row(expr_name)
        try:
            # Type column is column 2
            self.expr_table.item(r, 2).setText(str(typ))
        except Exception:
            self.expr_table.setItem(r, 2, QTableWidgetItem(str(typ)))

    def _set_expr_type_if_exists(self, expr_name, typ):
        """Set the Type column only if the expression row already exists. Do not create a new row."""
        r = self._find_expr_row(expr_name)
        if r is None:
            return
        try:
            self.expr_table.item(r, 2).setText(str(typ))
        except Exception:
            self.expr_table.setItem(r, 2, QTableWidgetItem(str(typ)))

    def _merge_update_and_refresh(self, name, payload, replace=False):
        """Merge payload into cached state for `name`.
        If replace=True, replace the cached object with payload (used for full 'state').
        If replace=False, update existing dict with payload fields.
        After merge, update the parent Value column, notify dialogs, update inline fields.
        """
        try:
            if replace:
                self.cache['states'][name] = payload
            else:
                cur = self.cache['states'].get(name, {})
                # ensure cur is a dict before update
                if not isinstance(cur, dict):
                    cur = {}
                if isinstance(payload, dict):
                    cur.update(payload)
                else:
                    # scalar replace
                    cur = payload
                self.cache['states'][name] = cur
            # store history for changed fields when payload is dict
            if isinstance(payload, dict):
                hist = self.cache.setdefault('history', {})
                h = hist.setdefault(name, {})
                for k, v in payload.items():
                    lst = h.setdefault(k, [])
                    lst.append((time.time(), v))
            # set parent Value column from merged cache
            st = self.cache['states'].get(name)
            try:
                if isinstance(st, dict):
                    self._set_expr_value(name, json.dumps(st))
                else:
                    self._set_expr_value(name, st)
            except Exception:
                pass
            # notify any open dialogs
            try:
                self._notify_dialog(name, self.cache['states'].get(name))
            except Exception:
                pass
            # refresh expanded inline field rows (fast path updates)
            QTimer.singleShot(0, lambda n=name: self._update_expanded_fields_from_state(n))
            # ensure full rows exist shortly after
            QTimer.singleShot(10, lambda n=name: self._refresh_expanded_expr(n))
        except Exception:
            pass

    def _expand_expr(self, expr_name):
        # Insert rows below the expression showing fields from cached state
        r = self._find_expr_row(expr_name)
        if r is None:
            r = self._ensure_expr_row(expr_name)
        if expr_name in self.expanded_exprs:
            return
        state = self.cache.get('states', {}).get(expr_name)
        base = self.expr_table
        base.blockSignals(True)
        insert_at = r + 1
        if isinstance(state, dict):
            for k in sorted(state.keys()):
                v = state[k]
                base.insertRow(insert_at)
                fk = QTableWidgetItem(str(k))
                # mark as field metadata
                fk.setData(Qt.UserRole, {'isField': True, 'parent': expr_name, 'field': k})
                # glyph blank for field row
                base.setItem(insert_at, 0, QTableWidgetItem(''))
                # put field name in Expression column
                base.setItem(insert_at, 1, fk)
                base.setItem(insert_at, 2, QTableWidgetItem(''))
                val_it = QTableWidgetItem(str(v))
                val_it.setFlags(val_it.flags() | Qt.ItemIsEditable)
                base.setItem(insert_at, 3, val_it)
                self._style_field_row(insert_at)
                insert_at += 1
        else:
            # scalar - show a single '<value>' row
            base.insertRow(insert_at)
            base.setItem(insert_at, 0, QTableWidgetItem(''))
            fk = QTableWidgetItem('<value>')
            fk.setData(Qt.UserRole, {'isField': True, 'parent': expr_name, 'field': '<value>'})
            base.setItem(insert_at, 1, fk)
            base.setItem(insert_at, 2, QTableWidgetItem(''))
            val_it = QTableWidgetItem(str(state))
            val_it.setFlags(val_it.flags() | Qt.ItemIsEditable)
            base.setItem(insert_at, 3, val_it)
            self._style_field_row(insert_at)
        # set parent glyph to expanded
        try:
            gi = base.item(r, 0)
            if gi is None:
                gi = QTableWidgetItem('▼')
                gi.setFlags(Qt.ItemIsEnabled)
                base.setItem(r, 0, gi)
            else:
                gi.setText('▼')
        except Exception:
            pass
        # ensure parent glyph shows expanded state
        try:
            gi = base.item(r, 0)
            if gi is None:
                gi = QTableWidgetItem('▼')
                gi.setFlags(Qt.ItemIsEnabled)
                base.setItem(r, 0, gi)
            else:
                gi.setText('▼')
        except Exception:
            pass
        base.blockSignals(False)
        self.expanded_exprs.add(expr_name)

    def _collapse_expr(self, expr_name):
        r = self._find_expr_row(expr_name)
        if r is None:
            return
        base = self.expr_table
        base.blockSignals(True)
        # remove rows immediately following r that are field rows (we marked them with UserRole isField)
        i = r + 1
        while i < base.rowCount():
            it = base.item(i, 1)
            if it is None:
                break
            meta = it.data(Qt.UserRole)
            if isinstance(meta, dict) and meta.get('isField'):
                base.removeRow(i)
                continue
            break
        base.blockSignals(False)
        try:
            self.expanded_exprs.remove(expr_name)
        except Exception:
            pass
        # reset parent glyph to collapsed
        try:
            gi = base.item(r, 0)
            if gi is None:
                gi = QTableWidgetItem('▶')
                gi.setFlags(Qt.ItemIsEnabled)
                base.setItem(r, 0, gi)
            else:
                gi.setText('▶')
        except Exception:
            pass


    def apply_filter(self):
        text = self.filter_input.text().strip().lower()
        top = self.tree.topLevelItemCount()
        for i in range(top):
            item = self.tree.topLevelItem(i)
            # show if name contains text or any child contains text
            def item_matches(it):
                if text == '':
                    return True
                if text in it.text(0).lower() or text in it.text(1).lower():
                    return True
                for j in range(it.childCount()):
                    if item_matches(it.child(j)):
                        return True
                return False
            visible = item_matches(item)
            item.setHidden(not visible)

    def _populate_ports(self):
        current = self.port_combo.currentText()
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo.clear()
        self.port_combo.addItems(ports)
        if current and current in ports:
            self.port_combo.setCurrentText(current)

    def _load_config(self):
        if CONFIG_PATH.exists():
            try:
                cfg = json.loads(CONFIG_PATH.read_text())
                port = cfg.get('port')
                baud = str(cfg.get('baud', '115200'))
                subs = cfg.get('subscriptions', [])
                self.subscriptions = set(subs)
                self.require_subscription = cfg.get('require_subscription', True)
                # do not load expressions from config anymore (we only persist subscriptions)
                if port:
                    # will be applied when ports enumerated
                    QTimer.singleShot(100, lambda: self.port_combo.setCurrentText(port))
                idx = self.baud_combo.findText(baud)
                if idx >= 0:
                    self.baud_combo.setCurrentIndex(idx)
                self.require_sub_chk.setChecked(self.require_subscription)
                self._log('Loaded config', cfg)
            except Exception as e:
                self._log('Failed load config:', e)

    def _save_config(self):
        cfg = {'port': self.port_combo.currentText(), 'baud': int(self.baud_combo.currentText()),
               'subscriptions': list(self.subscriptions), 'require_subscription': self.require_sub_chk.isChecked()}
        # save expressions
        try:
            exprs = []
            for r in range(self.expr_table.rowCount()):
                # columns: 0=glyph,1=expr,2=type,3=value
                item = self.expr_table.item(r, 1)
                if item is None:
                    continue
                # skip inline field rows (they store dict in UserRole) and placeholder rows
                try:
                    meta = item.data(Qt.UserRole)
                except Exception:
                    meta = None
                if isinstance(meta, dict) and meta.get('isField'):
                    continue
                # skip placeholder 'Add expression' rows
                text = item.text().strip()
                if not text:
                    continue
                if text.lower().startswith('add expression'):
                    continue
                it_type = self.expr_table.item(r, 2)
                it_val = self.expr_table.item(r, 3)
                exprs.append({'expr': text, 'type': it_type.text() if it_type else '', 'value': it_val.text() if it_val else ''})
            cfg['expressions'] = exprs
        except Exception:
            cfg['expressions'] = []
        try:
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
            self._log('Saved config')
        except Exception as e:
            self._log('Failed save config:', e)

    # Expressions table helpers
    def _add_expr_placeholder(self):
        # one placeholder row with disabled look
        try:
            base = self.expr_table
        except Exception:
            return
        # ensure there's exactly one placeholder as the last row
        base.blockSignals(True)
        if base.rowCount() == 0:
            base.setRowCount(1)
        # always ensure last row exists and shows placeholder text
        last = base.rowCount() - 1
        # choose placeholder text based on history
        ph = 'Add expression'
        try:
            if getattr(self, 'past_expr_count', 0):
                ph = f'Add expression ({self.past_expr_count} prev)'
        except Exception:
            ph = 'Add expression'
        # glyph blank, expression in col 1
        it = QTableWidgetItem(ph)
        it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsEditable)
        it.setData(Qt.UserRole, '')
        base.setItem(last, 1, it)
        base.setItem(last, 0, QTableWidgetItem(''))
        base.setItem(last, 2, QTableWidgetItem(''))
        base.setItem(last, 3, QTableWidgetItem(''))
        base.blockSignals(False)

    def _update_expr_placeholder_text(self):
        try:
            base = self.expr_table
        except Exception:
            return
        base.blockSignals(True)
        if base.rowCount() == 0:
            base.setRowCount(1)
        last = base.rowCount() - 1
        ph = 'Add expression'
        try:
            if getattr(self, 'past_expr_count', 0):
                ph = f'Add expression ({self.past_expr_count} prev)'
        except Exception:
            ph = 'Add expression'
        it = base.item(last, 1)
        if it is None:
            it = QTableWidgetItem(ph)
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsEditable)
            it.setData(Qt.UserRole, '')
            base.setItem(last, 1, it)
            base.setItem(last, 0, QTableWidgetItem(''))
            base.setItem(last, 2, QTableWidgetItem(''))
            base.setItem(last, 3, QTableWidgetItem(''))
        else:
            it.setText(ph)
            it.setData(Qt.UserRole, '')
        base.blockSignals(False)

    def _add_expression_row(self, expr, typ, val):
        try:
            base = self.expr_table
        except Exception:
            return
        base.blockSignals(True)
        # ensure placeholder exists
        if base.rowCount() == 0:
            base.setRowCount(1)
        last = base.rowCount() - 1
        row = max(0, last)
        # insert before the placeholder (which is last)
        base.insertRow(last)
        # glyph col
        g = QTableWidgetItem('▶')
        g.setFlags(Qt.ItemIsEnabled)
        base.setItem(last, 0, g)
        it = QTableWidgetItem(expr)
        it.setFlags(it.flags() | Qt.ItemIsEditable)
        it.setData(Qt.UserRole, expr)
        base.setItem(last, 1, it)
        # type non-editable
        t = QTableWidgetItem(typ)
        t.setFlags(Qt.ItemIsEnabled)
        base.setItem(last, 2, t)
        base.setItem(last, 3, QTableWidgetItem(val))
        base.blockSignals(False)

    def on_expr_double_clicked(self, row, col):
        try:
            base = self.expr_table
        except Exception:
            return
        # If placeholder clicked, start editing placeholder to add new expression
        last = base.rowCount()-1
        if row == last:
            # edit the placeholder expression (Expression column)
            self.expr_table.editItem(base.item(row, 1))
            return

    def on_expr_cell_clicked(self, row, col):
        """Handle single clicks on the expressions table.
        Clicking the glyph column (0) toggles expand/collapse. Clicking other columns is handled elsewhere.
        """
        try:
            base = self.expr_table
        except Exception:
            return
        # ignore clicks on placeholder row
        last = base.rowCount() - 1
        if row == last:
            return
        if col == 0:
            it = base.item(row, 1)
            if not it:
                return
            name = it.text().strip()
            if not name:
                return
            if name in self.expanded_exprs:
                self._collapse_expr(name)
            else:
                self._expand_expr(name)

    def on_expr_context_menu(self, pos):
        try:
            base = self.expr_table
        except Exception:
            return
        idx = base.indexAt(pos)
        if not idx.isValid():
            return
        r = idx.row()
        if r == base.rowCount()-1:
            return
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        act_del = m.addAction('Remove expression (and unsubscribe)')
        action = m.exec(base.viewport().mapToGlobal(pos))
        if action == act_del:
            # remove using helper so trailing field rows are removed too
            try:
                self._remove_expression_row(r)
            except Exception:
                pass
    def on_expr_cell_changed(self, row, col):
        base = self.expr_table
        # protect if table is empty
        if base.rowCount() == 0:
            return
        last = base.rowCount() - 1
        item = base.item(row, 1)  # get expression cell (Expression column)
        if item is None: return
        new = item.text().strip()

        # Check if this is a field row (we store dict in UserRole for field rows)
        fld_meta = None
        try:
            meta = base.item(row, 1).data(Qt.UserRole)
            if isinstance(meta, dict) and meta.get('isField'):
                fld_meta = meta
        except Exception:
            fld_meta = None
        if fld_meta is not None:
            # only value column edits (col 3) are meaningful for field rows
            if col != 3:
                return
            parent = fld_meta.get('parent')
            field = fld_meta.get('field')
            new_val_str = base.item(row, 3).text() if base.item(row, 3) else ''
            new_val = self._coerce_value(new_val_str)
            msg = {'id': f'set-{parent}-{field}', 'type': 'set', 'path': parent, 'changes': {field: new_val}}
            self._send(msg)
            self._log('Sent set for', parent, field, '->', new_val)
            return

        # If editing placeholder (last row) and Expression column
        if row == last and col == 1:
            # if user left it empty, keep placeholder visual
            if not new:
                base.blockSignals(True)
                item.setText('Add expression')
                item.setData(Qt.UserRole, '')
                base.blockSignals(False)
                return
            # user entered a new expression: insert as a real row before placeholder
            base.blockSignals(True)
            base.insertRow(last)
            # glyph
            g = QTableWidgetItem('▶')
            g.setFlags(Qt.ItemIsEnabled)
            base.setItem(last, 0, g)
            it = QTableWidgetItem(new)
            it.setFlags(it.flags() | Qt.ItemIsEditable)
            it.setData(Qt.UserRole, new)
            base.setItem(last, 1, it)
            base.setItem(last, 2, QTableWidgetItem('object'))
            base.setItem(last, 3, QTableWidgetItem(''))
            # restore placeholder text in the placeholder row
            placeholder = base.item(last+1, 1)
            if placeholder:
                placeholder.setText('Add expression')
                placeholder.setData(Qt.UserRole, '')
            base.blockSignals(False)
            # subscribe to new expression
            try:
                self._send({'id': 'sub-'+new, 'type': 'subscribe', 'path': new})
                self.subscriptions.add(new)
            except Exception as e:
                self._log('Failed subscribe for new expression', new, e)
            self._save_config()
            self._log('Added expression and subscribed to', new)
            return

        # Editing an existing expression row (rename)
        old = ''
        try:
            old_item = base.item(row, 1)
            if old_item:
                old = old_item.data(Qt.UserRole) or ''
        except Exception:
            old = ''
        # if new name empty -> remove row and unsubscribe
        if not new:
            if old in self.subscriptions:
                self._send({'id': 'unsub-'+old, 'type': 'unsubscribe', 'path': old})
                try:
                    self.subscriptions.remove(old)
                except Exception:
                    pass
            base.blockSignals(True)
            base.removeRow(row)
            base.blockSignals(False)
            self._save_config()
            self._log('Removed expression via empty edit and unsubscribed', old)
            return
        # if name changed, perform rename: unsubscribe old (if subscribed), subscribe new
        if new != old:
            if old in self.subscriptions:
                self._send({'id': 'unsub-'+old, 'type': 'unsubscribe', 'path': old})
                try:
                    self.subscriptions.remove(old)
                except Exception:
                    pass
                # subscribe new
                try:
                    self._send({'id': 'sub-'+new, 'type': 'subscribe', 'path': new})
                    self.subscriptions.add(new)
                except Exception as e:
                    self._log('Failed subscribe during rename', new, e)
            # update stored name metadata
            try:
                base.item(row, 1).setData(Qt.UserRole, new)
            except Exception:
                pass
            self._save_config()
            self._log('Renamed expression', old, '->', new)

    def _open_object_dialog(self, name):
        # reuse existing dialog if open
        if name in self.open_object_dialogs:
            d = self.open_object_dialogs[name]['dialog']
            try:
                d.raise_()
                d.activateWindow()
            except Exception:
                pass
            return
        # create a simple dialog showing fields and values
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QPushButton

        d = QDialog(self)
        d.setWindowTitle(f'Object: {name}')
        ly = QVBoxLayout(d)
        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(['Field', 'Value'])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked)
        ly.addWidget(table)

        # controls
        hl = QHBoxLayout()
        btn_refresh = QPushButton('Refresh')
        btn_close = QPushButton('Close')
        hl.addWidget(btn_refresh)
        hl.addWidget(btn_close)
        ly.addLayout(hl)

        def close_dialog():
            try:
                if name in self.open_object_dialogs:
                    del self.open_object_dialogs[name]
            except Exception:
                pass
            try:
                d.close()
            except Exception:
                pass

        btn_close.clicked.connect(close_dialog)

        def refresh():
            # request state from device
            self._send({'id': f'get-{name}', 'type': 'get', 'path': name})
        btn_refresh.clicked.connect(refresh)

        # context menu to delete a field
        def on_table_context(pos):
            idx = table.indexAt(pos)
            if not idx.isValid():
                return
            r = idx.row()
            field_item = table.item(r, 0)
            if not field_item:
                return
            field = field_item.text()
            from PySide6.QtWidgets import QMenu
            m = QMenu(d)
            act_del = m.addAction('Delete field')
            act = m.exec(table.viewport().mapToGlobal(pos))
            if act == act_del:
                # send delete
                self._send({'id': f'del-{name}-{field}', 'type': 'delete', 'path': name, 'field': field})
                # remove from table
                table.removeRow(r)

        table.setContextMenuPolicy(Qt.CustomContextMenu)
        table.customContextMenuRequested.connect(on_table_context)

        # handle edits in the dialog
        def on_table_item_changed(item):
            # only value column edits (col 1)
            if item.column() != 1:
                return
            r = item.row()
            fld = table.item(r, 0).text() if table.item(r, 0) else None
            if not fld:
                return
            new_val = item.text()
            newv = self._coerce_value(new_val)
            # send set for the field
            msg = {'id': f'set-{name}-{fld}', 'type': 'set', 'path': name, 'changes': {fld: newv}}
            self._send(msg)
            self._log('Dialog sent set for', name, fld, '->', newv)

        table.itemChanged.connect(on_table_item_changed)

        # function to populate/refresh dialog from state dict
        def update_state(state):
            try:
                table.blockSignals(True)
                table.setRowCount(0)
                if not isinstance(state, dict):
                    # show as single value
                    table.insertRow(0)
                    table.setItem(0, 0, QTableWidgetItem('<value>'))
                    table.setItem(0, 1, QTableWidgetItem(str(state)))
                else:
                    keys = sorted(state.keys())
                    for i, k in enumerate(keys):
                        v = state[k]
                        table.insertRow(i)
                        table.setItem(i, 0, QTableWidgetItem(str(k)))
                        table.setItem(i, 1, QTableWidgetItem(str(v)))
            finally:
                table.blockSignals(False)

        # store dialog and show
        self.open_object_dialogs[name] = {'dialog': d, 'table': table, 'update': update_state}
        # if we have cached state, populate immediately
        if name in self.cache.get('states', {}):
            st = self.cache['states'][name]
            QTimer.singleShot(0, lambda s=st: update_state(s))
        else:
            # request state
            self._send({'id': f'get-{name}', 'type': 'get', 'path': name})
        d.resize(400, 300)
        d.show()

    def _notify_dialog(self, name, state):
        # called when a new state/update arrives
        try:
            entry = self.open_object_dialogs.get(name)
            if not entry:
                return
            update_fn = entry.get('update')
            if update_fn:
                update_fn(state)
        except Exception:
            pass

    def toggle_connect(self):
        if self.ser and self.ser.is_open:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        port = self.port_combo.currentText()
        baud = int(self.baud_combo.currentText())
        if not port:
            QMessageBox.warning(self, 'No port', 'Please select a serial port')
            return
        try:
            self.ser = serial.Serial(port, baud, timeout=0.1)
        except Exception as e:
            QMessageBox.critical(self, 'Open failed', str(e))
            return
        self.reader_running = True
        self.reader_thread = threading.Thread(target=self._reader, daemon=True)
        self.reader_thread.start()
        self.connect_btn.setText('Disconnect')
        self._save_config()
        self._log('Connected', port, baud)
        # send any queued startup requests (discover/get) staggered to avoid bursts
        try:
            if self._pending_startup_requests:
                delay = 50
                for req_type, name in list(self._pending_startup_requests):
                    if req_type == 'discover':
                        QTimer.singleShot(delay, lambda n=name: self._send({'id': 'discover-'+n, 'type': 'discover', 'path': n}))
                    elif req_type == 'get':
                        QTimer.singleShot(delay, lambda n=name: self._send({'id': 'get-'+n, 'type': 'get', 'path': n}))
                    delay += 50
                self._pending_startup_requests.clear()
        except Exception:
            pass

        # also, send subscribe/discover/get for any configured subscriptions
        try:
            if self.subscriptions:
                delay = 50
                for name in list(self.subscriptions):
                    # send subscribe, then discover and get to populate schema/state
                    QTimer.singleShot(delay, lambda n=name: self._send({'id': 'sub-'+n, 'type': 'subscribe', 'path': n}))
                    QTimer.singleShot(delay + 30, lambda n=name: self._send({'id': 'discover-'+n, 'type': 'discover', 'path': n}))
                    QTimer.singleShot(delay + 80, lambda n=name: self._send({'id': 'get-'+n, 'type': 'get', 'path': n}))
                    delay += 80
        except Exception:
            pass

        # also request schema/state for built-in objects so they appear in the device tree
        try:
            builtins = ['laser', 'plasma']
            delay = 30
            for name in builtins:
                # only request if we don't already know the schema
                if name in self.cache.get('schemas', {}):
                    continue
                QTimer.singleShot(delay, lambda n=name: self._send({'id': 'discover-'+n, 'type': 'discover', 'path': n}))
                QTimer.singleShot(delay + 40, lambda n=name: self._send({'id': 'get-'+n, 'type': 'get', 'path': n}))
                delay += 60
        except Exception:
            pass
        except Exception:
            pass

    def disconnect(self):
        self.reader_running = False
        if self.reader_thread:
            self.reader_thread.join(timeout=0.5)
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.connect_btn.setText('Connect')
        self._log('Disconnected')

    def _reader(self):
        buf = ''
        while self.reader_running:
            try:
                b = self.ser.read()
            except Exception as e:
                self._log('Read error', e)
                break
            if not b:
                time.sleep(0.01)
                continue
            try:
                ch = b.decode('utf-8')
            except Exception:
                continue
            if ch == '\n':
                line = buf.strip()
                buf = ''
                if line:
                    # enqueue the received line for processing in the GUI thread
                    try:
                        self._incoming_queue.put(line)
                    except Exception:
                        pass
            else:
                buf += ch
                if len(buf) > 4000:
                    buf = ''

    def _process_incoming(self):
        # run in GUI thread via QTimer: drain queued serial lines and handle them
        try:
            while True:
                try:
                    line = self._incoming_queue.get_nowait()
                except Empty:
                    break
                try:
                    self._handle_message(line)
                except Exception:
                    pass
        except Exception:
            pass

    def _handle_message(self, line):
        self._log('RX', line)
        try:
            msg = json.loads(line)
        except Exception as e:
            self._log('Invalid json', e)
            return
        t = msg.get('type')
        if t == 'discover.response':
            if msg.get('found') and 'schema' in msg:
                s = msg['schema']
                name = s.get('name')
                # cache schema but do not display Type until the user subscribes
                self.cache['schemas'][name] = s
                # if user already subscribed, update Type to 'object'
                if name in self.subscriptions:
                    QTimer.singleShot(0, lambda n=name: self._set_expr_type(n, 'object'))
        elif t == 'subscribe.response':
            name = msg.get('path')
            # subscription confirmed by device — do not overwrite the object's Type column
            # keep Type showing the object's kind (object/state). If we know the schema, mark 'object', otherwise 'state' if we have state cached.
            # mark type only for subscribed objects; if schema already known, mark 'object', else mark 'state' until discover arrives
            def _mark_sub_type(n):
                if n in self.cache.get('schemas', {}):
                    self._set_expr_type_if_exists(n, 'object')
                elif n in self.cache.get('states', {}):
                    self._set_expr_type_if_exists(n, 'state')
            QTimer.singleShot(0, lambda n=name: _mark_sub_type(n))
        elif t == 'unsubscribe.response':
            name = msg.get('path')
            # device confirmed unsubscribe — remove locally and clear expecting
            try:
                if name in self.subscriptions:
                    self.subscriptions.remove(name)
            except Exception:
                pass
            try:
                if name in self.expecting:
                    del self.expecting[name]
            except Exception:
                pass
            QTimer.singleShot(0, lambda n=name: self._set_expr_type_if_exists(n, 'unsubscribed'))
            self._log('Device confirmed unsubscribe for', name)
        elif t == 'state':
            name = msg.get('path')
            # show scalar value or store/merge changes
            if 'value' in msg:
                # replace cached state with full snapshot and refresh UI
                self._merge_update_and_refresh(name, msg['value'], replace=True)
                # set Type only if subscribed: if subscribed+schema -> 'object', if subscribed but no schema -> 'state'
                def _choose_type(n):
                    if n in self.subscriptions:
                        if n in self.cache.get('schemas', {}):
                            self._set_expr_type_if_exists(n, 'object')
                        else:
                            self._set_expr_type_if_exists(n, 'state')
                    else:
                        # do not show type for unsubscribed objects
                        self._set_expr_type_if_exists(n, '')
                QTimer.singleShot(0, lambda n=name: _choose_type(n))
            elif 'changes' in msg:
                # merge partial changes into cache and refresh
                self._merge_update_and_refresh(name, msg['changes'], replace=False)
        elif t == 'update':
            name = msg.get('path')
            # ignore unsolicited updates unless subscribed (user requested this)
            require = self.require_sub_chk.isChecked()
            # allow updates if we recently asked for this object's state/schema
            now = time.time()
            expected_until = self.expecting.get(name, 0)
            if require and name not in self.subscriptions and expected_until < now:
                self._log('Ignored unsolicited update for', name)
                return
            if 'changes' in msg:
                # merge partial changes into cache and refresh
                # clear expecting flag if this was a response
                if name in self.expecting:
                    try:
                        del self.expecting[name]
                    except Exception:
                        pass
                self._merge_update_and_refresh(name, msg['changes'], replace=False)
        else:
            # other messages - ignore by default
            pass

    def on_discover(self):
        name = self.object_input.text().strip()
        if not name:
            QMessageBox.warning(self, 'No object', 'Type an object name to discover')
            return
        # Request schema (discover) first, then request state (get).
        req_disc = {'id': 'discover-'+name, 'type': 'discover', 'path': name}
        self._send(req_disc)
        # small delay then ask for state so GUI shows fields and values
        QTimer.singleShot(50, lambda n=name: self._send({'id': 'get-'+n, 'type': 'get', 'path': n}))
        # remember object in config
        self._save_config()

    def on_subscribe(self):
        name = self.object_input.text().strip()
        if not name:
            QMessageBox.warning(self, 'No object', 'Type an object name to subscribe')
            return
        # add to subscription list and send subscribe request
        self.subscriptions.add(name)
        req = {'id': 'sub-'+name, 'type': 'subscribe', 'path': name}
        self._send(req)
        self._save_config()
        self._log('Subscribed to', name)
        # request schema then immediate state so the object appears in the tree
        self._send({'id': 'discover-'+name, 'type': 'discover', 'path': name})
        QTimer.singleShot(100, lambda n=name: self._send({'id': 'get-'+n, 'type':'get', 'path': n}))

    def on_unsubscribe(self):
        name = self.object_input.text().strip()
        if not name:
            QMessageBox.warning(self, 'No object', 'Type an object name to unsubscribe')
            return
        if name in self.subscriptions:
            self.subscriptions.remove(name)
        # also clear any short-lived expecting window so updates are ignored immediately
        try:
            if name in self.expecting:
                del self.expecting[name]
        except Exception:
            pass
        req = {'id': 'unsub-'+name, 'type': 'unsubscribe', 'path': name}
        self._send(req)
        self._save_config()
        self._log('Unsubscribed from', name)

    def _send(self, obj):
        if not self.ser or not self.ser.is_open:
            self._log('Not connected')
            return
        try:
            s = json.dumps(obj, separators=(',',':')) + '\n'
            self.ser.write(s.encode('utf-8'))
            # if this is a discover/get/subscribe for a path, accept responses briefly
            try:
                t = obj.get('type')
                p = obj.get('path')
                if t in ('discover','get','subscribe') and p:
                    self.expecting[p] = time.time() + 3.0
            except Exception:
                pass
            self._log('TX', s.strip())
        except Exception as e:
            self._log('Send error', e)

    def _show_schema(self, name, schema):
        # normalize to top-level object name
        obj, _ = self._split_path(name)
        items = self.tree.findItems(obj, Qt.MatchExactly, 0)
        if items:
            item = items[0]
            # keep existing children but clear types/values to rebuild schema view
            item.takeChildren()
        else:
            item = QTreeWidgetItem([obj, 'object'])
            self.tree.addTopLevelItem(item)
        fields = schema.get('fields', [])
        for f in fields:
            # show field name and type (value will be filled by state)
            fi = QTreeWidgetItem([f.get('name','?'), f.get('type','?')])
            fi.setFlags(fi.flags() | Qt.ItemIsEditable)
            item.addChild(fi)
        item.setExpanded(True)

    def _show_state(self, name, state):
        # Accept both 'object' or 'object.field' paths
        obj, parts = self._split_path(name)
        items = self.tree.findItems(obj, Qt.MatchExactly, 0)
        if items:
            item = items[0]
        else:
            item = QTreeWidgetItem([obj, 'state'])
            self.tree.addTopLevelItem(item)

        # If state is a dict (full state), update or create children accordingly
        if isinstance(state, dict):
            # if schema known, try to preserve child order/types
            schema = self.cache['schemas'].get(obj)
            # remove children that are not in schema or state? We'll keep and update existing
            # update or create for each key in state
            for k, v in state.items():
                # handle arrays specially
                if isinstance(v, list):
                    # create container child
                    container = self._set_field_item_value(obj, k, '<array>')
                    # clear children
                    container.takeChildren()
                    total = len(v)
                    # add items and a 'load more' if needed; here we show first 50
                    chunk = 50
                    for i, val in enumerate(v[:chunk]):
                        child = QTreeWidgetItem([str(i), str(val)])
                        container.addChild(child)
                    if total > chunk:
                        more = QTreeWidgetItem([f'Load more (0..{chunk-1})', f'{chunk}/{total}'])
                        # store metadata
                        more.setData(0, Qt.UserRole, {'obj': obj, 'field': k, 'offset': chunk, 'limit': chunk, 'total': total})
                        container.addChild(more)
                else:
                    self._set_field_item_value(obj, k, v)
        else:
            # scalar state: if path included field parts, set that field; otherwise set top-level value
            if parts:
                field_name = parts[-1]
                self._set_field_item_value(obj, field_name, state)
            else:
                # set top-level display value
                item.setText(1, str(state))
        item.setExpanded(True)

    def _on_load_more(self, meta):
        # meta contains obj, field, offset, limit
        obj = meta.get('obj')
        field = meta.get('field')
        offset = meta.get('offset', 0)
        limit = meta.get('limit', 50)
        # request slice from device
        path = f'{obj}'
        req = {'id': f'get-{obj}-{field}-{offset}', 'type': 'get', 'path': path, 'offset': offset, 'limit': limit}
        self._send(req)
        self._log('Requested slice', obj, field, offset, limit)

    def _split_path(self, path):
        # returns (top_level_object, [subpath parts])
        if not path:
            return ('', [])
        parts = path.split('.')
        return (parts[0], parts[1:])

    def _set_field_item_value(self, obj_name, field_name, value):
        # ensure top-level object
        items = self.tree.findItems(obj_name, Qt.MatchExactly, 0)
        if items:
            obj_item = items[0]
        else:
            obj_item = QTreeWidgetItem([obj_name, 'state'])
            self.tree.addTopLevelItem(obj_item)
        # search for existing child
        for i in range(obj_item.childCount()):
            ch = obj_item.child(i)
            if ch.text(0) == field_name:
                ch.setText(1, str(value))
                return ch
        # not found, create
        fi = self._create_field_item(field_name, value)
        obj_item.addChild(fi)
        return fi

    def _mark_unsolicited(self, obj_name):
        # visually mark an object as coming from unsolicited update
        items = self.tree.findItems(obj_name, Qt.MatchExactly, 0)
        if not items:
            return
        obj_item = items[0]
        # gray out the object's text
        obj_item.setForeground(0, QColor('gray'))
        obj_item.setToolTip(0, 'Unsolicited data (not subscribed)')
        # also mark children
        for i in range(obj_item.childCount()):
            ch = obj_item.child(i)
            ch.setForeground(0, QColor('gray'))
            ch.setForeground(1, QColor('gray'))
            ch.setToolTip(0, 'Unsolicited field value')

    def _ensure_object_item(self, name):
        items = self.tree.findItems(name, Qt.MatchExactly, 0)
        if items:
            return items[0]
        it = QTreeWidgetItem([name, 'state'])
        self.tree.addTopLevelItem(it)
        return it

    def on_add_object(self):
        name = self.object_input.text().strip()
        if not name:
            QMessageBox.warning(self, 'No object', 'Type an object name to add')
            return
        self._ensure_object_item(name)
        self._log('Added object placeholder', name)
        # automatically request schema and state for newly added object
        self._send({'id': 'discover-'+name, 'type': 'discover', 'path': name})
        QTimer.singleShot(50, lambda n=name: self._send({'id': 'get-'+n, 'type': 'get', 'path': n}))

    def on_item_expanded(self, item):
        # Only act for top-level objects
        if item.parent() is not None:
            return
        name = item.text(0)
        # If we have no cached state or the item has no children, request state
        if name not in self.cache.get('states', {}) or item.childCount() == 0:
            req = {'id': f'get-{name}', 'type': 'get', 'path': name}
            self._send(req)
            self._log('Requested state for', name)

    def _on_tree_item_activated(self, item, col):
        data = item.data(0, Qt.UserRole)
        if isinstance(data, dict) and 'obj' in data:
            self._on_load_more(data)

    def _create_field_item(self, key, val):
        # if val is a dict/struct create nested children
        if isinstance(val, dict):
            fi = QTreeWidgetItem([key, 'struct'])
            for k, v in val.items():
                child = self._create_field_item(k, v)
                fi.addChild(child)
            fi.setExpanded(True)
            return fi
        else:
            vstr = str(val)
            fi = QTreeWidgetItem([key, vstr])
            # make value column editable so user can change it
            fi.setFlags(fi.flags() | Qt.ItemIsEditable)
            return fi

    def on_item_changed(self, item, column):
        # only act when value column changed (column 1) and item has a parent (field)
        if column != 1:
            return
        parent = item.parent()
        if not parent:
            return
        # find object name as top-level ancestor
        obj = parent
        while obj.parent() is not None:
            obj = obj.parent()
        obj_name = obj.text(0)
        field_name = item.text(0)
        new_val_str = item.text(1)
        # try to coerce to number or boolean
        new_val = self._coerce_value(new_val_str)
        # send set message
        msg = {'id': f'set-{obj_name}-{field_name}', 'type': 'set', 'path': obj_name, 'changes': {field_name: new_val}}
        self._send(msg)
        self._log('Sent set for', obj_name, field_name, '->', new_val)

    def _coerce_value(self, s):
        if s.lower() in ('true','false'):
            return s.lower() == 'true'
        try:
            if '.' in s:
                return float(s)
            return int(s)
        except Exception:
            return s

    def on_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return
        parent = item.parent()
        if not parent:
            return
        # show simple menu with Delete Field
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        act_del = menu.addAction('Delete field')
        act_hist = menu.addAction('Show history')
        action = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if action == act_del:
            # find object and field
            obj = parent
            while obj.parent() is not None:
                obj = obj.parent()
            obj_name = obj.text(0)
            field_name = item.text(0)
            # send delete request
            msg = {'id': f'del-{obj_name}-{field_name}', 'type': 'delete', 'path': obj_name, 'field': field_name}
            self._send(msg)
            self._log('Sent delete for', obj_name, field_name)
        elif action == act_hist:
            # show history dialog
            hist = self.cache.get('history', {}).get(obj_name, {}).get(field_name, [])
            from PySide6.QtWidgets import QDialog, QVBoxLayout, QTextEdit
            d = QDialog(self)
            d.setWindowTitle(f'History {obj_name}.{field_name}')
            ly = QVBoxLayout(d)
            te = QTextEdit()
            te.setReadOnly(True)
            for ts, val in hist:
                te.append(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}: {val}")
            ly.addWidget(te)
            d.resize(400,300)
            d.exec()

def main():
    app = QApplication(sys.argv)
    w = LiveWatchGUI()
    w.show()
    sys.exit(app.exec())

if __name__ == '__main__':
    main()
