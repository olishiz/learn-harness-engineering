#!/usr/bin/env python3
"""Render translated README illustrations with portable, shaped glyph outlines.

Dependencies: pip install fonttools uharfbuzz python-bidi cairosvg
Fonts are downloaded to a cache, verified against fonts.json, and never embedded
as external links. English artwork and all README prose remain untouched.
"""
from pathlib import Path
from xml.sax.saxutils import escape
import argparse
import hashlib
import io
import json
import re
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from functools import lru_cache
import cairosvg
import uharfbuzz as hb
from bidi import algorithm as bidi
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.boundsPen import BoundsPen

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
COPY = json.loads((HERE / 'translations.json').read_text())
MANIFEST = json.loads((HERE / 'fonts.json').read_text())
WIDTH = 1600

COLORS = {
    'light': dict(ink='#191918', accent='#D95C41', bar='#C14E36', muted='#73716C', line='#D8D6D0', soft='#F7F6F2', bg='#FFFFFF'),
    'dark': dict(ink='#EAE5DF', accent='#E07A64', bar='#B8503C', muted='#A8A4A0', line='#37404B', soft='#1B2027', bg='#0D1117'),
}

ICONS = {
    'task': '<path d="M-26-35H12L28-19V35H-26ZM12-35V-19H28M-15-10H16M-15 1H16M-15 12H7"/>',
    'files': '<path d="M-28-29H8L23-14V34H-28ZM8-29V-14H23M-16-4H11M-16 8H11M-16 20H4M-14-39H33V23"/>',
    'agent': '<rect x="-31" y="-27" width="62" height="54" rx="3"/><path d="M-12-8L-21 0L-12 8M12-8L21 0L12 8M5-14L-5 14M-15-39V-27M0-39V-27M15-39V-27M-15 27V39M0 27V39M15 27V39M-43-15H-31M-43 0H-31M-43 15H-31M31-15H43M31 0H43M31 15H43"/>',
    'check': '<path d="M0-38L30-25V3C30 21 15 33 0 41C-15 33-30 21-30 3V-25ZM-13 0L-3 10L17-12"/>',
    'state': '<ellipse cx="0" cy="-29" rx="29" ry="11"/><path d="M-29-29V26C-29 41 29 41 29 26V-29M-29-10C-29 5 29 5 29-10M-29 8C-29 23 29 23 29 8"/>',
    'scope': '<rect x="-34" y="-31" width="68" height="62"/><path d="M-24-18H-17M-10-18H24M-24-3H-17M-10-3H24M-24 12H-17M-10 12H12"/>',
    'lifecycle': '<path d="M-30-8A31 31 0 0 1 28-13M28-13V-32M28-13H9M30 8A31 31 0 0 1-28 13M-28 13V32M-28 13H-9M0-17V0L15 9"/>',
    'problem': '<rect x="-33" y="-29" width="66" height="55"/><path d="M-22-16L-11-7L-22 2M-4 2H10M-13 36H13M0 26V36M22-15V0M22 8V10"/>',
    'repo': '<path d="M-37-24H-6L3-13H37V30H-37ZM-23-1H22M-23 11H10"/>',
    'connect': '<rect x="-37" y="-24" width="26" height="48"/><rect x="11" y="-24" width="26" height="48"/><path d="M-11 0H11M4-7L11 0L4 7M-29-12H-18M-29 0H-18M-29 12H-18M18-12H29M18 0H29M18 12H29"/>',
    'feedback': '<path d="M-35-29H35V15H11L-3 29V15H-35ZM-21-14H21M-21-2H13"/>',
    'system': '<rect x="-35" y="-32" width="70" height="64"/><path d="M-35-10H35M-13-10V32M-24-22H-19M-11-22H-6M2-22H7M-1 4H23M-1 17H15"/>',
    'graph': '<rect x="-35" y="-32" width="20" height="20"/><rect x="15" y="-32" width="20" height="20"/><rect x="-10" y="18" width="20" height="20"/><path d="M-25-12V3H0V18M25-12V3H0M-15-22H15"/>',
}

class Font:
    def __init__(self, path):
        self.tt = TTFont(path)
        if 'fvar' in self.tt:
            axes = {a.axisTag: (500 if a.axisTag == 'wght' else a.defaultValue) for a in self.tt['fvar'].axes}
            self.tt = instantiateVariableFont(self.tt, axes, inplace=True)
        self.tt.flavor = None
        data = io.BytesIO()
        self.tt.save(data)
        self.hb = hb.Font(hb.Face(data.getvalue()))
        self.upem = self.tt['head'].unitsPerEm
        self.hb.scale = (self.upem, self.upem)
        self.glyphs = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()
        self.cmap = self.tt.getBestCmap()

    @lru_cache(maxsize=None)
    def outline(self, gid):
        glyph = self.glyphs[self.order[gid]]
        pen = SVGPathPen(self.glyphs, ntos=lambda v: format(v, ".2f").rstrip("0").rstrip(".") if v else "0")
        glyph.draw(pen)
        bounds = BoundsPen(self.glyphs)
        glyph.draw(bounds)
        return pen.getCommands(), bounds.bounds


class Typesetter:
    def __init__(self, fonts, locale):
        self.primary = fonts[{'zh-CN': 'NotoSansSC.otf', 'zh-TW': 'NotoSansTC.otf',
                              'ja-JP': 'NotoSansJP.otf', 'ko-KR': 'NotoSansKR.otf',
                              'ar-SA': 'NotoSansArabic.ttf'}.get(locale, 'NotoSans.ttf')]
        self.fallback = fonts['NotoSans.ttf']
        self.rtl = locale == 'ar-SA'

    @lru_cache(maxsize=None)
    def shape(self, text):
        text = unicodedata.normalize('NFC', text)
        storage = bidi.get_empty_storage()
        storage['base_level'] = bidi.get_base_level(text)
        storage['base_dir'] = 'R' if storage['base_level'] else 'L'
        bidi.get_embedding_levels(text, storage, False, False)
        for i, char in enumerate(storage['chars']): char['index'] = i
        bidi.explicit_embed_and_overrides(storage, False)
        bidi.resolve_weak_types(storage, False)
        bidi.resolve_neutral_types(storage, False)
        bidi.resolve_implicit_levels(storage, False)
        bidi.reorder_resolved_levels(storage, False)
        runs = []
        for char in storage['chars']:
            ch = char['ch']
            font = self.primary if ord(ch) in self.primary.cmap else self.fallback
            assert ord(ch) in font.cmap, f'Missing glyph: {ch} in {text}'
            key = (char['level'], font)
            if not runs or runs[-1][0] != key: runs.append((key, []))
            runs[-1][1].append(char)
        output, total = [], 0
        for (level, font), chars in runs:
            logical = ''.join(c['ch'] for c in sorted(chars, key=lambda c: c['index']))
            buf = hb.Buffer(); buf.add_str(logical); buf.guess_segment_properties()
            buf.direction = 'rtl' if level % 2 else 'ltr'
            hb.shape(font.hb, buf)
            for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
                assert info.codepoint != 0, f'Unshaped glyph: {logical}'
                output.append((font, info.codepoint, total + pos.x_offset/font.upem, pos.y_offset/font.upem))
                total += pos.x_advance/font.upem
        return output, total

    def width(self, text, size): return self.shape(text)[1]*size

    def lines(self, text, size, max_width):
        # CJK can wrap between characters; other scripts wrap at word boundaries.
        tokens = list(text) if any('\u3000' <= c <= '\u9fff' for c in text) else re.findall(r'\S+\s*', text)
        lines, line = [], ''
        for token in tokens:
            proposed = line + token
            if line and self.width(proposed.rstrip(), size) > max_width:
                lines.append(line.strip()); line = token.lstrip()
            else: line = proposed
        if line.strip(): lines.append(line.strip())
        return lines


class Figure:
    def __init__(self, name, variant, height, locale, typesetter, title, desc):
        self.name, self.variant, self.height, self.locale = name, variant, height, locale
        self.type = typesetter
        self.mirror = typesetter.rtl
        self.c = COLORS[variant]
        self.bounds = []
        self.outlines = {}
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="{height}" viewBox="0 0 1600 {height}" role="img" aria-labelledby="title desc" lang="{locale}">',
            f'<title id="title">{escape(title)}</title><desc id="desc">{escape(desc)}</desc>',
            f'<rect width="1600" height="{height}" fill="{self.c["bg"]}"/>',
            f'<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="{self.c["muted"]}"/></marker></defs>',
        ]

    def text(self, text, x, y, size=24, color='ink', anchor='middle', max_width=None, min_size=None, max_lines=1, line_height=None):
        if self.mirror:
            x = WIDTH-x
            anchor = {'start': 'end', 'end': 'start'}.get(anchor, anchor)
        current = size
        minimum = min_size or size*.8
        while max_width and self.type.width(text,current) > max_width and current > minimum:
            current = max(minimum, current-1)
        lines = self.type.lines(text,current,max_width) if max_width and max_lines>1 else [text]
        assert len(lines)<=max_lines, (self.locale,self.name,text,lines)
        for i,line in enumerate(lines):
            glyphs,width = self.type.shape(line)
            width *= current
            assert not max_width or width <= max_width+.01, (self.locale,text,width,max_width)
            start = x-width/2 if anchor=='middle' else x-width if anchor=='end' else x
            baseline = y + (i-(len(lines)-1)/2)*(line_height or current*1.25)
            self.parts.append(f'<g fill="{self.c.get(color,color)}" aria-label="{escape(line)}">')
            for font,gid,dx,dy in glyphs:
                commands,bounds = font.outline(gid)
                if not commands: continue
                scale = current/font.upem
                px,py = start+dx*current,baseline-dy*current
                if commands not in self.outlines: self.outlines[commands] = f'glyph-{len(self.outlines)}'
                self.parts.append(f'<use href="#{self.outlines[commands]}" transform="translate({px:.4f} {py:.4f}) scale({scale:.6f} {-scale:.6f})"/>')
                a,b,c,d = bounds
                self.bounds.append((px+a*scale,py-d*scale,px+c*scale,py-b*scale,line))
            self.parts.append('</g>')

    def label(self,text,x,y,size,max_width,max_lines=2,color='ink'):
        self.text(text,x,y,size,color,max_width=max_width,min_size=size*.9,max_lines=max_lines,line_height=size*1.25)

    def rect(self,x,y,w,h,fill='bg',stroke=None):
        if self.mirror:x=WIDTH-x-w
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{self.c[fill]}" stroke="{self.c[stroke] if stroke else "none"}" stroke-width="1.8"/>')

    def path(self,d,color='muted',arrow=False,width=1.8):
        mirror = ' transform="translate(1600 0) scale(-1 1)"' if self.mirror else ''
        self.parts.append(f'<path{mirror} d="{d}" fill="none" stroke="{self.c[color]}" stroke-width="{width}"'+(' marker-end="url(#arrow)"' if arrow else '')+'/>')

    def arrow(self,x1,x2,y):self.path(f'M{x1} {y}H{x2}',arrow=True)

    def icon(self,key,x,y,scale=1):
        if self.mirror:x=WIDTH-x
        self.parts.append(f'<g transform="translate({x} {y}) scale({scale})" fill="none" stroke="{self.c["accent"]}" stroke-width="2" stroke-linecap="square" stroke-linejoin="miter">{ICONS[key]}</g>')

    def heading(self,title,subtitle=None):
        self.text(title,800,74,40,max_width=1440)
        if subtitle:self.text(subtitle,800,117,23,'muted',max_width=1440)

    def add_outlines(self):
        if self.outlines:
            self.parts.append('<defs>' + ''.join(f'<path id="{name}" d="{d}"/>' for d,name in self.outlines.items()) + '</defs>')
            self.outlines = {}

    def save(self,out,preview=None):
        self.add_outlines()
        self.parts.append('</svg>')
        bad=[b for b in self.bounds if b[0]<24 or b[1]<20 or b[2]>1576 or b[3]>self.height-20]
        assert not bad,(self.locale,self.name,bad)
        source='\n'.join(self.parts)+'\n'; ET.fromstring(source)
        suffix='-dark' if self.variant=='dark' else ''
        target=out/f'{self.name}{suffix}.svg'; target.write_text(source)
        if self.variant=='light' and 'compact' not in self.name:
            cairosvg.svg2png(bytestring=source.encode(),write_to=str(target.with_suffix('.png')),output_width=2400)
        if preview:
            dest=preview/self.locale; dest.mkdir(parents=True,exist_ok=True)
            cairosvg.svg2png(bytestring=source.encode(),write_to=str(dest/f'{self.name}{suffix}.png'),output_width=880)


def diagram(name,variant,locale,t,typesetter):
    height={'pattern':690,'subsystems':700,'learning-path':980,'session-lifecycle':650}[name]
    title=t[{'pattern':'pattern.title','subsystems':'subsystems.title','learning-path':'road.title','session-lifecycle':'session.title'}[name]]
    f=Figure('harness-'+name,variant,height,locale,typesetter,title,title)
    if name=='pattern':
        f.heading(title); f.rect(380,146,1140,284,stroke='accent')
        for x,icon,a,b in [(175,'task','task','you'),(570,'files','read','instructions.state'),(965,'agent','exec','one.feature'),(1375,'check','passes','stops')]:
            f.icon(icon,x,233); f.label(t[a],x,322,28,270); f.label(t[b],x,384,22,270,color='muted')
        for a,b in [(236,499),(636,894),(1031,1304)]:f.arrow(a,b,233)
        f.rect(380,430,1140,42,fill='bar'); f.text(t['governs'],950,459,24,'#FFFFFF',max_width=1060)
        f.path('M950 474V506',arrow=True); f.rect(80,525,1440,125,fill='soft')
        for i,(a,b) in enumerate(zip(['instructions','scope','state','verification','lifecycle'],['what.order','one.time','progress.history','checks','start.handoff'])):
            x=224+288*i; f.label(t[a],x,562,25,250); f.label(t[b],x,613,21,250,color='muted')
            if i:f.path(f'M{80+288*i} 543V629',color='line',width=1)
    elif name=='subsystems':
        f.heading(title); f.rect(80,161,1440,390,stroke='accent')
        data=[('instructions','files',['AGENTS.md','CLAUDE.md','feature_list · docs/']),
              ('state','state',['progress.md','feature_list · git log',t['handoff']]),
              ('verification','check',[t['tests.lint'],t['types.smoke'],t['e2e']]),
              ('scope','scope',[t['one.time'],t['done']]),
              ('lifecycle','lifecycle',[t['init.start'],t['clean.end'],t['safe.commit']])]
        for i,(key,icon,lines) in enumerate(data):
            x=224+288*i; f.icon(icon,x,246); f.label(t[key],x,325,25,250)
            for j,line in enumerate(lines):f.label(line,x,385+56*j,21,250,color='muted')
            if i:f.path(f'M{80+288*i} 189V526',color='line',width=1)
        f.rect(80,551,1440,44,fill='bar');f.text(t['harness'],800,582,24,'#FFFFFF',max_width=1380)
        f.text(t['model.decides'],800,653,23,'muted',max_width=1440)
    elif name=='learning-path':
        f.heading(title,t['road.subtitle'])
        icons=['problem','repo','connect','feedback','check','system','lifecycle','graph']
        codes=['L01–L02 · P01','L03–L04 · P02','L05–L06 · P03','L07–L08 · P04','L09–L10 · P05','L11–L12 · P06','L13 · P07','L14 · P08']
        for row,y in [(0,170),(1,554)]:
            f.rect(80,y,1440,310,stroke='accent')
            for col in range(4):
                i=row*4+col; x=260+360*col
                f.text(f'{i+1:02d}',x,y+38,20,'accent'); f.icon(icons[i],x,y+110,scale=.86)
                f.label(t[f'phase.{i+1}'],x,y+184,27,310)
                f.text(codes[i],x,y+240,22,'accent')
                f.label(t[f'phase.sub.{i+1}'],x,y+283,20,310,color='muted')
                if col<3:f.arrow(x+65,x+286,y+110)
        f.path('M1340 482V514H260V552',arrow=True)
        f.rect(80,864,1440,42,fill='bar');f.text(t['road.total'],800,893,24,'#FFFFFF',max_width=1380)
    else:
        f.heading(title);f.rect(80,156,1440,355,stroke='accent')
        for i,(key,icon,a,b) in enumerate([('start','files','read.init','load.state'),('select','scope','unfinished','only.feature'),('execute','agent','implement.verify','evidence'),('wrap','lifecycle','state.commit','restart')]):
            x=260+360*i;f.icon(icon,x,261,scale=.86);f.label(t[key],x,346,29,310)
            f.label(t[a],x,404,22,310,color='muted');f.label(t[b],x,465,21,310,color='muted')
            if i<3:f.arrow(x+67,x+285,261)
        f.path('M1025 246V207H935V236',arrow=True); f.text(t['fix.rerun'],980,192,18,'muted',max_width=300)
        f.rect(80,511,1440,42,fill='bar');f.text(t['transition'],800,540,24,'#FFFFFF',max_width=1380)
        f.text(t['checks.pass'],800,610,23,'muted',max_width=1440)
    return f


def wordmark(variant,compact,locale,t,typesetter):
    name='harness-wordmark'+('-compact' if compact else '')
    path=ROOT/'assets/readme'/f'{name}{"-dark" if variant=="dark" else ""}.svg'
    root=ET.fromstring(path.read_text())
    ns='{http://www.w3.org/2000/svg}'
    for node in list(root):
        if node.tag==ns+'g' and node.get('aria-label') in ['AI coding agents','work reliably.']:root.remove(node)
        if node.tag==ns+'desc':node.text='Learn Harness Engineering. '+t['tag.agent']+' '+t['tag.reliable']
    root.set('lang',locale)
    f=Figure(name,variant,550 if compact else 470,locale,typesetter,'','')
    f.parts=[];f.mirror=False
    if compact:
        # The compact lockup leaves enough room for two balanced localized lines.
        f.text(t['tag.agent'],84,455,34,'muted',anchor='start',max_width=1400)
        f.text(t['tag.reliable'],84,494,34,'accent',anchor='start',max_width=1400)
        # Move the existing rule down to keep its breathing room.
        for node in root:
            if node.tag==ns+'path' and '509' in node.get('d',''):node.set('d',node.get('d').replace('509','532'))
        root.set('height','570');root.set('viewBox','0 0 1600 570');f.height=570
        for node in root:
            if node.tag==ns+'rect' and node.get('height')=='550':node.set('height','570')
    else:
        f.text(t['tag.agent'],1328,303,33,'muted',max_width=375,min_size=25,max_lines=2,line_height=35)
        f.text(t['tag.reliable'],1328,374,33,'accent',max_width=375,min_size=25,max_lines=2,line_height=35)
    f.add_outlines()
    for node in ET.fromstring('<svg>'+''.join(f.parts)+'</svg>'):root.append(node)
    source=ET.tostring(root,encoding='unicode')
    # Preserve the default SVG namespace rather than adding ns0 prefixes.
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    f.parts=[ET.tostring(root,encoding='unicode')[:-6]]
    return f


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font-dir',type=Path,default=Path.home()/'.cache/learn-harness-engineering/readme-fonts')
    parser.add_argument('--preview-dir',type=Path)
    parser.add_argument('--locale',action='append',choices=sorted(COPY))
    args=parser.parse_args();args.font_dir.mkdir(parents=True,exist_ok=True)
    fonts={}
    for filename,meta in MANIFEST.items():
        target=args.font_dir/filename
        if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest()!=meta['sha256']:
            subprocess.run(['curl','-fLsS','--retry','2','-o',str(target),meta['url']],check=True)
        assert hashlib.sha256(target.read_bytes()).hexdigest()==meta['sha256'],filename
        if target.suffix in ['.ttf','.otf']:fonts[filename]=Font(target)
        else:(ROOT/'assets/readme/licenses'/filename).write_text('\n'.join(line.rstrip() for line in target.read_text().splitlines())+'\n')
    for locale in args.locale or COPY:
        t=COPY[locale];typesetter=Typesetter(fonts,locale)
        out=ROOT/'assets/readme'/locale;out.mkdir(exist_ok=True)
        for variant in ['light','dark']:
            for compact in [False,True]:wordmark(variant,compact,locale,t,typesetter).save(out,args.preview_dir)
            for name in ['pattern','subsystems','learning-path','session-lifecycle']:
                diagram(name,variant,locale,t,typesetter).save(out,args.preview_dir)
        readme=ROOT/'docs-readme'/locale/'README.md'
        content=readme.read_text()
        content=re.sub(r'../../assets/readme/(?:'+re.escape(locale)+r'/)?(harness-[^"\s]+)',r'../../assets/readme/'+locale+r'/\1',content)
        readme.write_text(content)
        print(locale,'— 12 SVGs, 5 PNGs; README references updated',flush=True)

if __name__=='__main__':main()
