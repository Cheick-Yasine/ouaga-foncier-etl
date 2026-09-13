"""Inspecte le HTML local sans ouvrir Facebook ni afficher scripts, cookies ou formulaires."""
import argparse
import json
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse, parse_qs

UI = re.compile(r'recherch|search|plus récent|most recent|recent posts|filtr|trier', re.I)
VOID = {'input', 'img', 'br', 'hr', 'meta', 'link', 'source', 'area', 'base', 'wbr', 'embed', 'param', 'col', 'track'}


def clean(value):
    value = re.sub(r'https?://\S+', '[lien]', value)
    value = re.sub(r'[\w.+-]+@[\w.-]+', '[email]', value)
    value = re.sub(r'\d{6,}', '[nombre]', value)
    return ' '.join(value.split())[:160]


class Diagnostic(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.controls = []
        self.roles = Counter()
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        ignored = bool(self.hidden or tag in {'script', 'style', 'noscript'} or attrs.get('type') == 'hidden')
        node = {'tag': tag, 'attrs': attrs, 'text': '', 'ignored': ignored}
        if ignored:
            self.hidden += 1
        else:
            if attrs.get('role'):
                self.roles[attrs['role']] += 1
        if tag in VOID:
            self.finish(node)
        else:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.hidden:
            return
        for node in self.stack:
            if len(node['text']) < 200:
                node['text'] += data + ' '

    def handle_endtag(self, tag):
        indexes = [i for i, n in enumerate(self.stack) if n['tag'] == tag]
        if not indexes:
            return
        while len(self.stack) > indexes[-1]:
            self.finish(self.stack.pop())

    def finish(self, node):
        if node['ignored']:
            self.hidden -= 1
            return
        a = node['attrs']
        relevant = UI.search(' '.join([node['text'], a.get('aria-label', ''), a.get('placeholder', '')]))
        is_control = node['tag'] in {'a','button','input','h1','h2','h3','label'} or a.get('role') in {'button','link','searchbox','textbox','switch','checkbox','heading'}
        if not relevant or not is_control or len(self.controls) >= 60:
            return
        item = {'tag': node['tag']}
        for key in ('role', 'aria-label', 'placeholder', 'aria-checked', 'type'):
            if a.get(key):
                item[key] = clean(a[key])
        if node['tag'] != 'input' and node['text'].strip():
            item['texte'] = clean(node['text'])
        if a.get('href'):
            url = urlparse(a['href'])
            if not url.netloc or url.hostname == 'facebook.com' or (url.hostname or '').endswith('.facebook.com'):
                item['chemin_lien'] = url.path
                query = parse_qs(url.query)
                for key in ('q', 'query'):
                    if query.get(key) in (['terrain'], ['parcelle']):
                        item[key] = query[key][0]
        if item not in self.controls:
            self.controls.append(item)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('html', type=Path)
    args = parser.parse_args()
    diagnostic = Diagnostic()
    diagnostic.feed(args.html.read_text(encoding='utf-8'))
    print(json.dumps({'roles': dict(diagnostic.roles), 'controles_recherche': diagnostic.controls}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
