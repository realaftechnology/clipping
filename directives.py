"""Checked actions: the only things a chat directive can change (central hub, 2026-11-24).

An editor's chat agent never gets a shell or file access. It can only propose actions from this catalogue. The hub
validates each one against the requester's role and decides whether it applies straight away or waits for Matt; the
worker that owns the editor validates it again before applying it. Every applied action returns what it replaced,
so it can be undone.

Hard exclusions, risk flags, the review gate, accounts, posting credentials and the Output Spec's locked look
(static captions, no highlighted word) are not in the catalogue, so no chat message can change them.
Matt, 2026-11-24: "I still want the agent to be able to learn, and I want all new lessons to be applied to all agents...
The job still needs to get done."

Standard library only; Python 3.9 (the editing Mac) and the hub both import it.
"""
import json, os, re, time, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, 'fonts.json')

ROLES = ['viewer', 'reviewer', 'director', 'admin']


def rank(role):
    return ROLES.index(role) if role in ROLES else -1


def fonts():
    return sorted(k for k in json.load(open(FONTS)) if not k.startswith('_'))


HEX = re.compile(r'^#[0-9A-Fa-f]{6}$')
HM = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')

# style.json fields a directive may set, with how each value is checked. Anything else in style.json stays as is.
STYLE_FIELDS = {
    'caption_font': ('font', 'Caption font'),
    'hook_font': ('font', 'Hook font'),
    'caption_size': ((40, 110), 'Caption size in px'),
    'hook_size': ((40, 110), 'Hook size in px'),
    'caption_y': ((900, 1700), 'Caption position from the top, px of 1920'),
    'hook_top': ((120, 800), 'Hook position from the top, px of 1920'),
    'hook_bg': ('hex', 'Hook box colour'),
    'hook_color': ('hex', 'Hook text colour'),
}

# What each action is, for the chat agent's prompt and the dashboard.
CATALOGUE = {
    'set_style': 'Change one look setting of this editor: {"field": one of %s, "value": ...}. Fonts must be one of the '
                 'approved fonts. Only this editor changes; style is the one thing editors differ on.' % sorted(STYLE_FIELDS),
    'add_note': 'Add a standing behaviour note for this editor only, read before it picks every hook, song, trim and caption: '
                '{"text": "..."}. Only for how THIS editor works or looks (e.g. "keep hooks under 8 words"). Anything about '
                'which clips are acceptable, quality or subject matter is network-wide: use add_rule instead.',
    'add_rule': 'Propose a network-wide rule every editor follows (content, clip choice, quality, editing): '
                '{"text": "...", "kind": "scrap" (which clips to skip) or "edit" (how to edit)}. Waits for Matt\'s approval.',
    'set_posting': 'Change how this editor posts: {"slots": ["08:00", ...] (1-8 daily times, 24 h)} and/or '
                   '{"max_review": 1-30} and/or {"enabled": false} to pause it or true to resume, and/or '
                   '{"auto_post": false} to send every clip to review (true needs Matt).',
    'reedit': 'Re-edit one of this editor\'s clips that hasn\'t posted yet: {"edit_id": n, "hook": "new hook" or "", '
              '"song": "file name from the catalog" or "", "what_changed": "..."}. The new version goes to review.',
}

# Which actions need Matt before they apply. Content rules always do (Matt, 2026-11-24: content directives wait for his
# approval). For a director (Grok Bot), anything beyond style and re-edits does too: "human approval for sensitive changes".
def needs_approval(action, role, params, owner=False):
    a = action
    if a == 'add_rule':
        return True
    if a == 'set_posting':
        if params.get('auto_post') is True or params.get('enabled') is True:
            return not owner
        return rank(role) < rank('admin') and not (set(params) == {'enabled'} and params['enabled'] is False)
    if a == 'add_note':
        return rank(role) < rank('admin')
    return False  # set_style, reedit: applied straight away, logged and undoable


class Invalid(ValueError):
    pass


def validate(action, params, role):
    """Check one proposed action. Returns the cleaned params, or raises Invalid with a plain reason."""
    if rank(role) < rank('director'):
        raise Invalid('only directors and admins can give directives')
    if action not in CATALOGUE:
        raise Invalid(f'"{action}" is not an action editors can take')
    if not isinstance(params, dict):
        raise Invalid('params must be an object')
    p = {}
    if action == 'set_style':
        f, v = params.get('field'), params.get('value')
        if f not in STYLE_FIELDS:
            raise Invalid(f'{f!r} is not a style setting chat can change (allowed: {", ".join(sorted(STYLE_FIELDS))})')
        kind = STYLE_FIELDS[f][0]
        if kind == 'font':
            match = next((x for x in fonts() if x.lower() == str(v).strip().lower()), None)
            if not match:
                raise Invalid(f'{v!r} is not an approved font (approved: {", ".join(fonts())})')
            v = match
        elif kind == 'hex':
            if not HEX.match(str(v)):
                raise Invalid(f'{v!r} is not a colour like #FFFFFF')
            v = str(v).upper()
        else:
            lo, hi = kind
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise Invalid(f'{f} must be a whole number')
            if not lo <= v <= hi:
                raise Invalid(f'{f} must be between {lo} and {hi}')
        p = {'field': f, 'value': v}
    elif action == 'add_note':
        t = str(params.get('text') or '').strip()
        if not 3 <= len(t) <= 500:
            raise Invalid('a note is 3 to 500 characters')
        p = {'text': t}
    elif action == 'add_rule':
        t = str(params.get('text') or '').strip()
        if not 3 <= len(t) <= 500:
            raise Invalid('a rule is 3 to 500 characters')
        k = params.get('kind', 'scrap')
        if k not in ('scrap', 'edit'):
            raise Invalid('kind is "scrap" or "edit"')
        p = {'text': t, 'kind': k}
    elif action == 'set_posting':
        if 'slots' in params:
            s = params['slots']
            if not isinstance(s, list) or not 1 <= len(s) <= 8 or not all(isinstance(x, str) and HM.match(x) for x in s):
                raise Invalid('slots is a list of 1-8 times like "08:00"')
            p['slots'] = sorted(set(s))
        if 'max_review' in params:
            try:
                m = int(params['max_review'])
            except (TypeError, ValueError):
                raise Invalid('max_review must be a whole number')
            if not 1 <= m <= 30:
                raise Invalid('max_review must be between 1 and 30')
            p['max_review'] = m
        for k in ('enabled', 'auto_post'):
            if k in params:
                if not isinstance(params[k], bool):
                    raise Invalid(f'{k} must be true or false')
                p[k] = params[k]
        if not p:
            raise Invalid('nothing to change: give slots, max_review, enabled or auto_post')
    elif action == 'reedit':
        try:
            p['edit_id'] = int(params.get('edit_id'))
        except (TypeError, ValueError):
            raise Invalid('edit_id must be a clip number')
        p['hook'] = str(params.get('hook') or '').strip()[:140]
        p['song'] = str(params.get('song') or '').strip()[:200]
        p['what_changed'] = str(params.get('what_changed') or '').strip()[:300]
        if not (p['hook'] or p['song']):
            raise Invalid('a re-edit needs a new hook or a new song')
    return p


def describe(action, params):
    """One plain line for the chat and the audit log."""
    if action == 'set_style':
        return f"{STYLE_FIELDS[params['field']][1]} → {params['value']}"
    if action == 'add_note':
        return f"Note: {params['text']}"
    if action == 'add_rule':
        return f"Network rule ({'clip choice' if params['kind'] == 'scrap' else 'editing'}): {params['text']}"
    if action == 'set_posting':
        bits = []
        if 'slots' in params:
            bits.append('post times ' + ', '.join(params['slots']))
        if 'max_review' in params:
            bits.append(f"at most {params['max_review']} clips waiting for review")
        if 'enabled' in params:
            bits.append('resume' if params['enabled'] else 'pause')
        if 'auto_post' in params:
            bits.append('auto-post safe clips' if params['auto_post'] else 'send every clip to review')
        return 'Posting: ' + '; '.join(bits)
    if action == 'reedit':
        what = [f'hook "{params["hook"]}"' if params['hook'] else '', f"song {params['song']}" if params['song'] else '']
        return f"Re-edit clip {params['edit_id']}: " + ', '.join(x for x in what if x)
    return action


# ---------- applying (on the worker that owns the editor) ----------
def _editor_dir(eid):
    if not re.match(r'^[a-z0-9-]{1,40}$', eid or ''):
        raise Invalid('bad editor id')
    d = os.path.join(HERE, 'editors', eid)
    if not os.path.exists(os.path.join(d, 'editor.json')):
        raise Invalid(f'editor {eid} does not run on this machine')
    return d


def _write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def notes_path(eid):
    return os.path.join(_editor_dir(eid), 'notes.json')


def read_notes(eid):
    try:
        return json.load(open(notes_path(eid))).get('notes', [])
    except (OSError, ValueError, Invalid):
        return []


def active_notes(eid):
    return [n for n in read_notes(eid) if n.get('active', True)]


def apply(eid, action, params, by, directive_id):
    """Apply one validated action to an editor on this machine. Returns {'result': text, 'undo': data}.
    Re-edits are applied by the caller (they render), so they're not handled here."""
    d = _editor_dir(eid)
    if action == 'set_style':
        path = os.path.join(d, 'style.json')
        s = json.load(open(path))
        old = s.get(params['field'])
        s[params['field']] = params['value']
        _write_json(path, s)
        return {'result': f"{STYLE_FIELDS[params['field']][1]}: {old} → {params['value']}",
                'undo': {'field': params['field'], 'value': old}}
    if action == 'add_note':
        notes = read_notes(eid)
        nid = f'n{directive_id}' if directive_id else uuid.uuid4().hex[:8]
        notes.append({'id': nid, 'text': params['text'], 'by': by, 'at': time.time(), 'active': True, 'directive': directive_id})
        _write_json(notes_path(eid), {'_note': 'Behaviour notes from chat directives (central hub). The decide step reads the active ones.',
                                      'notes': notes})
        return {'result': f"Note added: {params['text']}", 'undo': {'note_id': nid}}
    if action == 'set_posting':
        path = os.path.join(d, 'editor.json')
        e = json.load(open(path))
        old = {k: e.get(k) for k in params}
        e.update(params)
        _write_json(path, e)
        return {'result': describe(action, params), 'undo': old}
    raise Invalid(f'{action} is not applied on the editor directly')


def undo(eid, action, undo_data):
    """Put back what an applied action replaced."""
    d = _editor_dir(eid)
    if action == 'set_style':
        path = os.path.join(d, 'style.json')
        s = json.load(open(path))
        if undo_data.get('value') is None:
            s.pop(undo_data['field'], None)
        else:
            s[undo_data['field']] = undo_data['value']
        _write_json(path, s)
        return f"{STYLE_FIELDS[undo_data['field']][1]} back to {undo_data.get('value')}"
    if action == 'add_note':
        notes = read_notes(eid)
        for n in notes:
            if n['id'] == undo_data.get('note_id'):
                n['active'] = False
        _write_json(notes_path(eid), {'_note': 'Behaviour notes from chat directives (central hub). The decide step reads the active ones.',
                                      'notes': notes})
        return 'Note removed'
    if action == 'set_posting':
        path = os.path.join(d, 'editor.json')
        e = json.load(open(path))
        for k, v in undo_data.items():
            if k in ('slots', 'max_review', 'enabled', 'auto_post'):
                if v is None:
                    e.pop(k, None)
                else:
                    e[k] = v
        _write_json(path, e)
        return 'Posting settings put back'
    raise Invalid(f'{action} cannot be undone here')
