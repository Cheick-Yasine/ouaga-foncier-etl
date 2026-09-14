"""Événements numériques uniquement, sans contenu de publication ni secret."""
import json
import os

FIELDS = {'scroll', 'network', 'graphql', 'dom', 'captured', 'selected', 'archived'}


def emit(event, group, **counts):
    path = os.environ.get('OUAGA_PROGRESS_FILE')
    if not path:
        return
    record = {'event': event, 'group': str(group)}
    record.update({k: v for k, v in counts.items() if k in FIELDS and type(v) is int and v >= 0})
    with open(path, 'a', encoding='utf-8') as out:
        out.write(json.dumps(record) + '\n')


def render(record, groups):
    event = record.get('event')
    group = record.get('group', '')
    if event not in {'open', 'scroll', 'archived'} or not isinstance(group, str) or not group.isascii() or not group.isdigit():
        return None
    counts = {k: v for k, v in record.items() if k in FIELDS and type(v) is int and v >= 0}
    number = groups.setdefault(group, len(groups) + 1)
    prefix = f'Groupe {number}'
    if event == 'open':
        return prefix + ' | ouverture'
    if event == 'archived' and 'archived' in counts:
        return prefix + f" | archivage brut Neon confirmé : {counts['archived']} publications dans le fichier (avant filtrage des annonces)"
    required = {'scroll', 'network', 'graphql', 'dom', 'captured', 'selected'}
    if event == 'scroll' and required <= counts.keys():
        return prefix + (' | scroll {scroll} | réponses réseau={network} | GraphQL={graphql}'
                         ' | éléments DOM={dom} | publications capturées={captured}'
                         ' | retenues pour ce groupe={selected}').format(**counts)
    return None
