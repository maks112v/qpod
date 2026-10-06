#!/usr/bin/env python3
"""Execute the actual patched MIPS callback with stock filter and mocked UI services.
Requires unicorn==2.1.4. Does not emulate the entire device or flash hardware.
"""
import json, math, pathlib, re, struct, sys
from unicorn import Uc, UcError, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN, UC_HOOK_CODE, UC_HOOK_BLOCK
from unicorn.mips_const import *
import sys; sys.path.insert(0, sys.path[0] + '/../tools')  # tools/ first: test/build.py must import tools/build.py
from build import segments, symbols, BASE, SCRATCH, HOOKS, IPOD_HOOKS, WM_PAINT_LEAF, FUNCTIONS, GLOBALS, CONTEXT_DATA, ROOT, source_sha256, sha, PRIVATE_FUNCTIONS, VERSIONS, VERSION
B=pathlib.Path(sys.argv[1] if len(sys.argv)>1 else 'build')
manifest=json.loads((B/'manifest.json').read_text())
if manifest.get('source_sha256') != source_sha256():
    raise SystemExit(f'{B}/manifest.json does not match the current patch sources; rebuild into a fresh directory and pass it here')
for name,key in (('demo','demo_sha256'),('stock-demo','stock_demo_sha256'),('patch.bin','patch_sha256')):
    if sha((B/name).read_bytes()) != manifest.get(key):
        raise SystemExit(f'{B/name} does not match manifest.json; rebuild into a fresh directory')
variant = manifest.get('variant')
v = VERSIONS.get(variant, '')
assert v and manifest['version'] == (v[:-1] + v[-1].lower() if manifest.get('dev') else v), 'Wrong variant/version'
assert (manifest.get('compact_code') != []) == (variant == 'ipod')
from ipod import INC, O  # patch/offsets.inc and its integer #defines
DC=int(re.search(r'^#define DOUBLE_CLICK_MS (\d+)$',(ROOT/'patch/navigation.c').read_text(),re.M)[1])  # centre double-press window
# iPod accent presets: {gradient top, bottom, light tone, red tone, highlight} per Accent setting value.
ACCENTS=[tuple(int(v,16) for v in g) for g in re.findall(r'\{ 0x(\w+), 0x(\w+), 0x(\w+), 0x(\w+), 0x(\w+) \}',INC)]
def color_t(rgb): return 0xff000000|(rgb&255)<<16|(rgb>>8&255)<<8|rgb>>16
O_GLYPH=re.search(r'#define BT_GLYPH "(\w+)"',INC)[1]  # the plain Bluetooth glyph a codec badge fades into
CONFIG={}  # config.ini [IPOD] keys a new Machine starts with; the payload reads them on first use
FILL,SHADE,OUTLINE=((O[a]<<24)|O[c] for a,c in (('FILL_ALPHA','FILL_RGB'),('SHADE_ALPHA','FILL_RGB'),('OUTLINE_ALPHA','OUTLINE_RGB')))
LCD_COLORS=(0x9abcdef0,0x12345678)
syms=symbols(B/'stock-demo')
syms.update(PRIVATE_FUNCTIONS)
VG_MOCKS=[n for n in syms if n.startswith(('vgcanvas_','vg_gradient_'))]

def _fp64_fix():
    """The stock binary is -mfp64 (FR=1); Unicorn's MIPS32 FPU only implements FR=0, so
    L-format conversions are rewritten to their W equivalents when an image is loaded. Every
    value the traversed canvas code converts fits 32 bits, so the results are identical."""
    data=(B/'demo').read_bytes(); fixes=[]
    for _,(t,o,v,_,f,m,flags,_) in segments(data):
        if t!=1 or not flags&1: continue
        for off in range(o,o+f,4):
            w=struct.unpack_from('<I',data,off)[0]
            if w>>26&0x3f==0x11 and w>>21&0x1f==0x15:
                fixes.append((v+off-o,(w&~(0x1f<<21))|(0x14<<21)))
    return fixes
FP64_FIX=_fp64_fix()
REGS=[UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3]
SAVED=[UC_MIPS_REG_S0,UC_MIPS_REG_S1,UC_MIPS_REG_S2,UC_MIPS_REG_S3,
       UC_MIPS_REG_S4,UC_MIPS_REG_S5,UC_MIPS_REG_S6,UC_MIPS_REG_S7,UC_MIPS_REG_FP]

def signed(x): return x if x<0x80000000 else x-0x100000000

class Machine:
    def __init__(self, patched=True):
        self.u=Uc(UC_ARCH_MIPS, UC_MODE_MIPS32|UC_MODE_LITTLE_ENDIAN)
        data=(B/('demo' if patched else 'stock-demo')).read_bytes()
        for _,(t,o,v,_,f,m,flags,_) in segments(data):
            if t!=1: continue
            start=v&~4095; end=(v+m+4095)&~4095
            self.u.mem_map(start,end-start)
            self.u.mem_write(v,data[o:o+f])
            # Payload text is execute-only; its top page holds the scratch cell.
            if v==BASE: self.u.mem_protect(start,SCRATCH-start,5)
        for v,w in FP64_FIX: self.u.mem_write(v,struct.pack('<I',w))
        self.u.mem_map(0x1000000,0x200000)
        self.u.mem_map(0x70000000,0x10000)
        self.next=0x1001000; self.nodes={}; self.calls=[]; self.animating=0; self.pressed=0
        self.top=0; self.wm=0x1000000; self.event=0x1000100
        self.strokes=[]; self.rounded=[]; self.bands=[]; self.icons=[]; self.letters=[]; self.font=None; self.vg_calls=[]; self.fake_vg=0; self.global_alpha=0
        self.rounded_fail=False
        self.allocs={}; self.config=dict(CONFIG); self.config_reads=[]
        self.rebind=None; self.on_click=None; self.glide=True
        self.timers={}; self.next_timer=1; self.timer_fail=False; self.clicks=[]; self.started=[]
        self.screens=[]
        self.slides={}; self.slide_fail=False; self.slide_on_fail=False; self.slide_callbacks={}
        self.canvas=0x1000200; self.lcd=0x1000300; self.now=1000
        self.word(self.canvas+O['CANVAS_LCD'],self.lcd)
        for off,v in zip(('LCD_FILL_COLOR','LCD_STROKE_COLOR'),LCD_COLORS): self.word(self.lcd+O[off],v)
        self.clip=(0,0,240,240)
        self.handlers={}
        for name in FUNCTIONS: self.handlers[syms[name]]=name
        # canvas_get_vgcanvas runs stock, down to the mocked lcd_get_vgcanvas, as the rounded wrappers need
        for name in ('slide_menu_item_width','slide_menu_on_scroll_done','canvas_get_vgcanvas',
                     'widget_animator_scroll_set_params','slide_menu_set_value','toolsTimeItoa','on_wm_keyup_fun'):
            self.handlers.pop(syms[name],None)
        self.mock('widget_is_instance_of','widget_animator_scroll_create','widget_animator_on',
                  'widget_set_focused','widget_layout_children','event_init','value_set_int')
        if patched:
            for name in ('paint','dispatch','paint_bg'):
                self.handlers[int(manifest['patch_symbols']['stock_'+name+'_trampoline'],16)]='stock_'+name
        self.mock('reset_poweroptions_timer','screen_action','enable_fb','usleep@GLIBC_2.0','sprintf@GLIBC_2.0',
                  'airplayGetFlag','playpause_quick_click','time@GLIBC_2.0','localtime@GLIBC_2.0',
                  'strlen@GLIBC_2.0','strrchr@GLIBC_2.0','strcasecmp@GLIBC_2.0','strncasecmp@GLIBC_2.0')
        self.image_size=(50,50)  # what widget_load_image decodes
        self.clock=(18,14)  # local (hour, minute) for time/localtime, or the one of them that fails
        self.handlers[syms['memcpy@GLIBC_2.0']]='memcpy'
        self.handlers[syms['memset@GLIBC_2.0']]='memset'
        self.mock('canvas_set_global_alpha')
        self.mock('sqrtf@GLIBC_2.0','sinf@GLIBC_2.0','acosf@GLIBC_2.0',prefix='float:')
        self.mock(*VG_MOCKS,prefix='vg:')
        self.code_hook=self.u.hook_add(UC_HOOK_CODE,self.hook)
        for name in GLOBALS: self.byte(syms[name],0)
        for name,size in CONTEXT_DATA.items(): self.u.mem_write(syms[name],bytes(size))
        self.word(syms['g_class_type'],0xf001)
        self.byte(syms['g_backlight_status'],1)
    def probe_canvas(self, lcd_type=1, vg=0, rect=(10,10,100,40), color=None):
        """Real stock canvas code runs; mock only the services it reaches."""
        for n in ('canvas_fill_rounded_rect','canvas_stroke_rounded_rect','canvas_set_fill_color'):
            del self.handlers[syms[n]]
        self.mock('lcd_get_vgcanvas'); self.mock('tk_calloc','tk_free',prefix='alloc:')
        for off,val in ((0x10,0),(0x14,0),(0x18,239),(0x1c,239)): self.word(self.canvas+off,val)
        self.word(self.lcd+0xd0,lcd_type)
        self.rounded_fail=False; self.fake_vg=vg
        self.rect=self.alloc(16); self.color=self.alloc(4)
        for i,val in enumerate(rect): self.word(self.rect+4*i,val)
        self.word(self.color,FILL if color is None else color)
    def lcd_colors(self): return tuple(self.get(self.lcd+O[off]) for off in ('LCD_FILL_COLOR','LCD_STROKE_COLOR'))
    def mock(self,*names,prefix=''):
        for n in names: self.handlers[syms[n]]=prefix+n
    def folder(self,path,w=None):
        """Set the browsed folder; with a surface, also scroll it home and repaint."""
        self.u.mem_write(syms['g_folder_path'],path.encode()+b'\0')
        if w: self.word(w+O['SCROLL_Y'],0); self.paint(w)
    def label(self,es,specs):
        """Give each entry None, a text, or a (text, subtitle label) pair."""
        for e,spec in zip(es,specs):
            if spec is None: continue
            text,*sub=spec if isinstance(spec,tuple) else (spec,)
            self.nodes[e]['text']=text
            if sub: self.nodes[e]['children']=[self.node('label',text=sub[0])]
    def panes(self,height=None):
        """Two four-entry scroll panes on album_page; returns (a, b, a entries, b entries)."""
        a=self.node(); b=self.node(); es=[]
        for s in (a,b):
            if height: self.word(s+O['W_H'],height)
            es.append([self.entry(s,i*48) for i in range(4)]); self.nodes[s]['children']=es[-1]
        self.top=self.node('window','album_page',[a,b])
        return a,b,*es
    def byte(self,a,v): self.u.mem_write(a,bytes([v]))
    def word(self,a,v): self.u.mem_write(a,struct.pack('<I',v&0xffffffff))
    def get(self,a): return struct.unpack('<I',self.u.mem_read(a,4))[0]
    def alloc(self,n=0x200): a=self.next; self.next+=n; return a
    def string(self,s):
        data=s.encode()+b'\0'
        a=self.alloc((len(data)+3)&~3); self.u.mem_write(a,data); return a
    def wide_string(self,s):
        data=(s+'\0').encode('utf-32-le')
        a=self.alloc(len(data)); self.u.mem_write(a,data); return a
    def text(self,a):
        if not a: return ''
        out=bytearray()
        while (c:=self.u.mem_read(a,1))!=b'\0': out+=c; a+=1
        return out.decode()
    def node(self,t='scroll_view',name='',children=(),visible=1,**kw):
        a=self.alloc(); self.nodes[a]=dict(type=t,name=name,children=list(children),visible=visible,enable=1,**kw)
        self.word(a+O['W_W'],240); self.word(a+O['W_H'],240); self.word(a+O['ROW_HEIGHT'],48)
        self.word(a+(O['VIEW_CONTENT_H'] if t=='scroll_view' else O['TABLE_ROWS']),960 if t=='scroll_view' else 100)
        self.byte(a+O['VIEW_VERTICAL'],1)
        return a
    def entry(self,parent,y=0):
        """A leafless tap target: emitter with one EVT_CLICK item, widget_y offset y, height 48."""
        a=self.node('list_item')
        em=self.alloc(4); it=self.alloc(0x28)
        self.word(a+O['W_EMITTER'],em); self.word(em,it); self.word(it+O['EMIT_TYPE'],O['EVT_CLICK'])
        self.word(a+O['W_PARENT'],parent); self.word(a+O['W_Y'],y); self.word(a+O['W_H'],48)
        return a
    def selected(self,w): return self.nodes[w].get('_ringnav_index',-1)
    def paint(self,w,gap=1000):
        """Stock order: background, then (children and) border; iPod hooks both."""
        if variant=='ipod':
            self.call(address=IPOD_HOOKS['widget_on_paint_background'][0],args=(w,self.canvas,0,0),gap=gap); gap=0
        return self.call(address=HOOKS['widget_on_paint_border'][0],args=(w,self.canvas,0,0),gap=gap,clear=variant!='ipod')
    def drawn(self): return self.rounded or self.strokes or self.bands
    def sel(self):
        """The selected row's rectangle, from the outline (inset 1) or the bar's bands."""
        if variant=='ipod':
            ys=[b[1] for b in self.bands]
            return (self.bands[0][0],min(ys),self.bands[0][2],max(ys)-min(ys)+1)
        x,y,w,h=self.rounded[0]['rect'] if self.rounded else self.strokes[0][:4]
        return (x-1,y-1,w+2,h+2)
    def touch(self): return self.call(address=HOOKS['on_wm_tsdown_before_fun'][0],event_type=O['EVT_POINTER_DOWN'])
    def click(self,w,gap=1000): return self.call(address=HOOKS['widget_dispatch'][0],args=(w,self.event,0,0),event_type=O['EVT_CLICK'],gap=gap)
    def hook(self,u,address,size,_):
        if address==syms['widget_animator_scroll_set_params']:
            assert u.reg_read(UC_MIPS_REG_A0) not in self.slides, 'retarget must pause first'
            self.calls.append(('widget_animator_scroll_set_params',*[u.reg_read(r) for r in REGS[:3]]))
        if address not in self.handlers: return
        name=self.handlers[address]
        if not name.startswith('stock_'):
            assert u.reg_read(UC_MIPS_REG_T9)==address, (name,'PIC call missing t9')
        a,b,c,d=[u.reg_read(r) for r in REGS]
        n=self.nodes.get(a,{})
        self.calls.append((name,a,b,c))
        if name=='memcpy': self.u.mem_write(a,bytes(self.u.mem_read(b,c))); ret=a
        elif name=='memset': self.u.mem_write(a,bytes([b&255])*c); ret=a
        elif name in ('table_row_create', 'list_item_create', 'button_create', 'image_create', 'view_create',
                       'hscroll_label_create', 'gif_image_create', 'label_create'):
            kind = {'gif_image_create': 'gif'}.get(name, name.removesuffix('_create'))
            ret = self.node(kind)
            for off, value in zip((O['W_X'], O['W_Y'], O['W_W'], O['W_H']),
                                  (b, c, d, self.get(u.reg_read(UC_MIPS_REG_SP)+16))):
                self.word(ret+off, value)
            self.word(ret+O['W_PARENT'], a)
            self.nodes[a]['children'].append(ret)
        elif name=='widget_set_name': n['name']=self.text(b); ret=0
        elif name=='widget_set_enable': n['enable']=b; ret=0
        elif name=='widget_on': n.setdefault('handlers',[]).append((b,c,d)); ret=len(n['handlers'])
        elif name=='widget_to_local':
            x,y=signed(self.get(b)),signed(self.get(b+4))
            while a:
                x-=signed(self.get(a+O['W_X'])); y-=signed(self.get(a+O['W_Y']))
                a=self.get(a+O['W_PARENT'])
            self.word(b,x); self.word(b+4,y); ret=0
        elif name=='widget_set_text_utf8': n['text']=self.text(b); ret=0
        elif name=='image_base_set_image': n['image']=self.text(b); ret=0
        elif name=='widget_use_style': n['style']=self.text(b); ret=0
        elif name.startswith('hscroll_label_set_') or name=='set_hscroll_label_attribute':
            n[name]=b if name!='set_hscroll_label_attribute' else True; ret=0
        elif name=='image_set_draw_type': ret=a
        elif name=='deque_at': ret=self.row_record
        elif name=='deque_size': ret=1
        elif name in ('tk_snprintf','snprintf@GLIBC_2.0'):
            fmt=self.text(c); values=[d]+[self.get(u.reg_read(UC_MIPS_REG_SP)+off) for off in (16,20,24)]
            if fmt=='%.*s': fmt,values='%s',[self.string(self.text(values[1]).encode()[:values[0]].decode())]  # track_name
            params=[self.text(value) if kind=='s' else value if kind in 'xXu' else signed(value)
                    for kind,value in zip(re.findall(r'%\d*([sdxXu])',fmt),values)]
            result=(fmt % tuple(params)).encode(); self.u.mem_write(a,result[:b-1]+b'\0'); ret=len(result)
        elif name=='strlen@GLIBC_2.0': ret=len(self.text(a).encode())
        elif name=='strrchr@GLIBC_2.0': i=self.text(a).encode().rfind(bytes([b&255])); ret=a+i if i>=0 else 0
        elif name in ('strcasecmp@GLIBC_2.0','strncasecmp@GLIBC_2.0'):  # ASCII case, as the C locale
            x,y=(self.text(v).encode().lower()[:c if name.startswith('strn') else None] for v in (a,b)); ret=(x>y)-(x<y)
        elif name=='sprintf@GLIBC_2.0':  # stock toolsTimeItoa's "%02d:%02d[:%02d]"
            fmt=self.text(b); values=(c,d,self.get(u.reg_read(UC_MIPS_REG_SP)+16))
            result=(fmt % tuple(signed(v) for v in values[:fmt.count('%')])).encode(); self.u.mem_write(a,result+b'\0'); ret=len(result)
        elif name=='time@GLIBC_2.0':
            ret=-1 if self.clock=='time' else 86400
            if a and ret!=-1: self.word(a,ret)
        elif name=='localtime@GLIBC_2.0':  # struct tm: sec, min, hour, ...
            ret=0
            if self.clock!='localtime':
                assert self.get(a)==86400; ret=self.alloc(44)
                for i,v in enumerate((59,self.clock[1],self.clock[0])): self.word(ret+4*i,v)
        elif name=='widget_set_children_layout':
            n['children_layout']=self.text(b)
            layout=self.alloc(32)
            self.word(layout+O['CHILDREN_LAYOUT_VTABLE'],O['DEFAULT_LAYOUT_VTABLE'])
            self.u.mem_write(layout+0x14,struct.pack('<HH',1,0))
            for param,off in (('xm','DEFAULT_LAYOUT_X_MARGIN'),('s','DEFAULT_LAYOUT_SPACING')):
                match=re.search(r'\b'+param+r'=(\d+)',self.text(b))
                self.byte(layout+O[off],int(match[1]) if match else 0)
            self.word(a+O['W_CHILDREN_LAYOUT'],layout); ret=0
        elif name=='widget_resize':
            self.word(a+O['W_W'],b); self.word(a+O['W_H'],c); ret=0
        elif name=='widget_vtable_on_layout_children': ret=3
        elif name in ('darray_init','darray_deinit','widget_layout_floating_children','widget_layout_self'): ret=0
        elif name=='widget_get_children_for_layout':
            children=[x for x in n['children'] if self.nodes[x]['visible']]
            values=self.alloc(4*len(children)+4)
            for i,child in enumerate(children): self.word(values+4*i,child)
            self.word(b,len(children)); self.word(b+8,values); ret=0
        elif name=='toolsReadConfig':
            key=self.text(c); self.config_reads.append((self.text(a),self.text(b),key,self.text(self.get(u.reg_read(UC_MIPS_REG_SP)+16))))
            value=self.config.get(key,self.config_reads[-1][3])  # stock copies the default when the key is missing
            self.u.mem_write(d,value.encode()+b'\0'); ret=1 if key in self.config else -1
        elif name=='tk_str_end_with': ret=self.text(a).endswith(self.text(b))
        elif name=='tk_str_start_with': ret=self.text(a).startswith(self.text(b))
        elif name=='strtol@GLIBC_2.0': t=re.match(r'\s*[-+]?\d+',self.text(a)); ret=int(t[0]) if t else 0
        elif name=='strstr@GLIBC_2.0': i=self.text(a).find(self.text(b)); ret=a+i if i>=0 else 0
        elif name=='bitmap_get_line_length': ret=self.get(a+8)
        elif name=='bitmap_lock_buffer_for_write': ret=self.get(a+0x14)  # a test bitmap keeps its pixels' address there
        elif name=='image_manager': ret=0x1000500
        elif name in ('widget_move_resize_ex','widget_move_resize'):
            for off,value in zip(('W_X','W_Y','W_W','W_H'),(b,c,d,self.get(u.reg_read(UC_MIPS_REG_SP)+16))):
                self.word(a+O[off],value)
            ret=0
        elif name=='widget_lookup':
            def lookup(w):
                if self.nodes[w].get('name')==self.text(b): return w
                for child in self.nodes[w]['children']:
                    found=lookup(child) if c else (child if self.nodes[child].get('name')==self.text(b) else 0)
                    if found: return found
                return 0
            ret=lookup(a) if a else 0
        elif name=='scroll_bar_cast': ret=a
        elif name=='scroll_bar_is_mobile': ret=n.get('type')=='scroll_bar_m'
        elif name=='widget_set_opacity': self.byte(a+0x34,b); ret=0
        elif name in ('widget_set_visible','widget_set_visible_only','widget_set_sensitive'):
            n['sensitive' if name=='widget_set_sensitive' else 'visible']=b; ret=0
        elif name=='widget_animator_prop_create':
            ret=self.alloc(0x80); self.word(ret+4,a); self.word(ret+O['ANIM_DURATION'],b)
            self.word(ret+0x24,c); assert self.text(self.get(u.reg_read(UC_MIPS_REG_SP)+16))=='opacity'
        elif name=='widget_animator_prop_set_params':
            assert self.get(u.reg_read(UC_MIPS_REG_SP)+16)==0 and self.get(u.reg_read(UC_MIPS_REG_SP)+20)==0
            ret=0
        elif name=='window_manager': ret=self.wm
        elif name=='window_manager_get_top_window': ret=self.top
        elif name=='window_manager_is_animating': ret=self.animating
        elif name=='window_manager_get_pointer_pressed': ret=self.pressed
        elif name=='widget_get_visible': ret=n.get('visible',0)
        elif name=='widget_get_type': ret=self.string(n.get('type',''))
        elif name=='widget_get_prop_str':
            # Stock text is VALUE_TYPE_WSTRING; value_str does not convert it to UTF-8.
            ret=0 if self.text(b)=='text' else self.string(n.get(self.text(b),''))
        elif name=='widget_get_text': ret=self.wide_string(n.get('text',''))
        elif name in ('widget_get_prop_bool','widget_get_prop_int'): ret=n.get(self.text(b),c)
        elif name=='mclGetLyricSize': ret=getattr(self,'lyric_size',0)
        elif name=='widget_count_children': ret=len(n['children'])
        elif name=='widget_get_child': ret=n['children'][b] if b<len(n['children']) else 0
        elif name=='widget_set_prop_int': n[self.text(b)]=signed(c); ret=0
        elif name=='pointer_event_init':
            self.word(a,b); self.word(a+0x10,c); ret=a
        elif name=='time_now_ms': ret=self.now & 0xffffffff
        elif name=='timer_add':
            ret=0 if self.timer_fail else self.next_timer
            if ret:
                self.next_timer+=1
                self.timers[ret]=(self.now+c,a,b,c)
        elif name=='timer_remove': self.timers.pop(a,None); ret=0
        elif name=='screen_action': self.screens.append(a); ret=1
        elif name=='player_start': self.started.append((a,b,c,d)); ret=1
        elif name in ('tk_strcmp','strcmp@GLIBC_2.0'): x,y=self.text(a),self.text(b); ret=0 if a and b and x==y else -1 if x<=y else 1
        elif name=='stock_dispatch':
            if self.get(b)==O['EVT_CLICK']:
                self.clicks.append(a)
                if self.on_click: self.on_click(a,b)
            ret=0
        elif name=='table_client_stop_animator_scroll': self.word(a+O['TABLE_ANIMATOR'],0); ret=0
        elif name=='widget_is_instance_of': ret=1
        elif name=='widget_set_focused': n['focused']=b; ret=0
        elif name=='event_init': self.word(a,b); ret=a
        elif name=='value_set_int': self.word(a,b); ret=a
        elif name=='widget_animator_scroll_create':
            ret=0 if self.slide_fail else self.alloc(0x80)
            if ret:
                self.word(ret+4,a); self.word(ret+O['ANIM_DURATION'],b)
                if self.nodes.get(a,{}).get('type')=='slide_menu': assert c==0 and d==O['SLIDE_EASING']
        elif name=='widget_animator_on':
            ret=0 if self.slide_on_fail else 1
            if ret: self.slide_callbacks[a]=(c,d)
        elif name=='widget_animator_pause': self.slides.pop(a,None); ret=0
        elif name=='widget_animator_destroy':
            self.slides.pop(a,None); self.slide_callbacks.pop(a,None); ret=0
        elif name=='widget_animator_start':
            if a in self.slide_callbacks:
                w=self.get(a+4)
                if self.nodes.get(w,{}).get('type')=='slide_menu':
                    assert self.get(a+O['ANIM_ELAPSED'])==self.get(a+O['ANIM_START_TIME'])==0
                    self.slides[a]=(self.now,self.get(a+O['ANIM_DURATION']),w,
                                    signed(self.get(a+0x68)),signed(self.get(a+O['ANIM_X_TO'])))
            ret=0
        elif name=='table_client_set_yoffset':
            assert self.get(a+O['TABLE_ANIMATOR'])==0
            self.word(a+O['TABLE_TOP'],b)
            if self.rebind: self.rebind(a,b)
            ret=0
        elif name=='scroll_view_set_offset':
            assert self.get(a+O['VIEW_ANIMATOR'])==0
            self.word(a+O['SCROLL_X'],b); self.word(a+O['SCROLL_Y'],c)
            ret=0
        elif name=='table_client_scroll_to':
            if self.glide:
                self.word(a+O['TABLE_TOP'],b)
                if self.rebind: self.rebind(a,b)
            else: self.word(a+O['TABLE_ANIMATOR'],0x1234)
            ret=0
        elif name=='canvas_get_clip_rect':
            for j,v in enumerate(self.clip): self.word(b+4*j,v)
            ret=0
        elif name=='canvas_set_clip_rect': self.clip=tuple(signed(self.get(b+4*j)) for j in range(4)); ret=0
        elif name=='canvas_set_fill_color': self.word(self.lcd+O['LCD_FILL_COLOR'],b); ret=0
        elif name=='canvas_set_stroke_color': self.word(self.lcd+O['LCD_STROKE_COLOR'],b); ret=0
        elif name=='canvas_set_global_alpha': self.global_alpha+=1; ret=0
        elif name in ('canvas_fill_rounded_rect','canvas_stroke_rounded_rect'):
            sp=u.reg_read(UC_MIPS_REG_SP)
            # The stock rounded calls set the matching LCD color on their CPU branch; model that
            # stricter side effect so a missing restore fails the state assertions below.
            kind='fill' if name=='canvas_fill_rounded_rect' else 'stroke'
            radius=self.get(sp+16); width=None if kind=='fill' else self.get(sp+20)
            self.rounded.append(dict(kind=kind,rect=tuple(signed(self.get(b+4*j)) for j in range(4)),
                bg=signed(c),color=self.get(d),radius=radius,width=width,clip=self.clip))
            if kind=='stroke' and (self.rounded_fail is True or self.rounded_fail==radius):
                ret=2   # a backend that declines to draw, as the stock no-vgcanvas path does
            else:
                self.word(self.lcd+(O['LCD_FILL_COLOR'] if kind=='fill' else O['LCD_STROKE_COLOR']),self.get(d))
                ret=0
        elif name=='widget_load_image':  # list_into is 50x50; image_size None fails the load
            ret=1 if self.image_size is None else 0
            if not ret: self.word(c,self.image_size[0]); self.word(c+4,self.image_size[1])
        elif name=='canvas_draw_icon': self.icons.append((signed(c),signed(d),self.clip)); ret=0
        elif name=='canvas_set_font': self.font=(self.text(b) if b else 'default',c); ret=0
        elif name=='canvas_set_text_color': self.word(self.lcd+O['LCD_TEXT_COLOR'],b); ret=0
        elif name=='canvas_draw_text_in_rect':
            self.letters.append(dict(text=''.join(chr(self.get(b+4*j)) for j in range(c)),
                rect=tuple(signed(self.get(d+4*j)) for j in range(4)),color=self.get(self.lcd+O['LCD_TEXT_COLOR']),font=self.font,
                align=(self.get(a+O['CANVAS_ALIGN_H']),self.get(a+O['CANVAS_ALIGN_V'])),clip=self.clip)); ret=0
        elif name=='canvas_fill_rect':
            self.bands.append((signed(b),signed(c),signed(d),signed(self.get(u.reg_read(UC_MIPS_REG_SP)+16)),
                               self.get(self.lcd+O['LCD_FILL_COLOR']),self.clip)); ret=0
        elif name in ('canvas_stroke_rect','lcd_stroke_rect'):
            h=self.get(u.reg_read(UC_MIPS_REG_SP)+16)
            clip=self.clip if name=='canvas_stroke_rect' else (self.get(self.canvas+0x10),self.get(self.canvas+0x14),self.get(self.canvas+0x18)-self.get(self.canvas+0x10)+1,self.get(self.canvas+0x1c)-self.get(self.canvas+0x14)+1)
            self.strokes.append((signed(b),signed(c),signed(d),signed(h),clip,self.get(self.lcd+O['LCD_STROKE_COLOR']))); ret=0
        elif name=='scroll_view_scroll_delta_to':
            if self.glide:
                self.word(a+O['SCROLL_X'],self.get(a+O['SCROLL_X'])+b); self.word(a+O['SCROLL_Y'],self.get(a+O['SCROLL_Y'])+c)
            else: self.word(a+O['VIEW_ANIMATOR'],0x1234)
            ret=0
        elif name=='lcd_get_vgcanvas': ret=self.fake_vg
        elif name.startswith('vg:'):
            self.vg_calls.append((name[3:],signed(a),signed(b)))
            ret=1 if name[3:]=='vgcanvas_save' else 0
        elif name.startswith('float:'):
            x=struct.unpack('<f',struct.pack('<I',u.reg_read(UC_MIPS_REG_F12)))[0]
            fn={'sqrtf@GLIBC_2.0':math.sqrt,'sinf@GLIBC_2.0':math.sin,'acosf@GLIBC_2.0':math.acos}[name[6:]]
            ret=struct.unpack('<I',struct.pack('<f',fn(x)))[0]
            u.reg_write(UC_MIPS_REG_F0,ret)
        elif name in ('alloc:tk_calloc','alloc:tk_free'):
            if name=='alloc:tk_calloc':
                size=a*b; p=self.alloc(size+16); self.allocs[p]=size; ret=p
            else:
                self.allocs.pop(a,None); ret=0
        else: ret=0
        # Clobber caller-saved registers to catch accidental ABI assumptions.
        for r in [UC_MIPS_REG_V1,*REGS,UC_MIPS_REG_T0,UC_MIPS_REG_T1,UC_MIPS_REG_T2,
                  UC_MIPS_REG_T3,UC_MIPS_REG_T4,UC_MIPS_REG_T5,UC_MIPS_REG_T6,
                  UC_MIPS_REG_T7,UC_MIPS_REG_T8,UC_MIPS_REG_T9]:
            u.reg_write(r,0xdeadbeef)
        u.reg_write(UC_MIPS_REG_V0,ret&0xffffffff)
        u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))
    def fast(self):
        """Hook stock code and the payload's trampolines only, so the payload's own loops (the
        Coverflow renderer) run at native emulator speed."""
        self.u.hook_del(self.code_hook)
        self.u.hook_add(UC_HOOK_CODE,self.hook,begin=0,end=BASE-1)
        self.u.hook_add(UC_HOOK_CODE,self.hook,begin=SCRATCH,end=0xffffffff)
        for a in list(self.handlers):
            if BASE<=a<SCRATCH: self.u.hook_add(UC_HOOK_CODE,self.hook,begin=a,end=a)
    budget=100000  # instructions per call
    def call(self,key=O['KEY_NEXT'],address=HOOKS['on_wm_keyup_before_fun'][0],args=None,event_type=0x114,gap=1000,stack=(),clear=True,debounce=False,count=None):
        # Independent input steps occur after the stock key debounce timer expires.
        if gap: self.advance(gap,clear=False)
        if not debounce: self.byte(0xa37c89,0)  # stock key filter latch
        if clear: self.calls=[]; self.strokes=[]; self.rounded=[]; self.bands=[]; self.icons=[]; self.letters=[]; self.vg_calls=[]
        self.word(self.event+O['EVENT_KEY'],key)
        self.word(self.event+O['EVENT_TYPE'],event_type)
        self.u.reg_write(UC_MIPS_REG_SP,0x7000f000)
        self.u.reg_write(UC_MIPS_REG_RA,0x1000000)
        self.u.reg_write(UC_MIPS_REG_T9,address)
        for i,v in enumerate(stack): self.word(0x7000f010+4*i,v)
        for r,v in zip(REGS,args or (self.wm,self.event,0,0)): self.u.reg_write(r,v&0xffffffff)
        for i,r in enumerate(SAVED): self.u.reg_write(r,0x12340000+i)
        self.u.emu_start(address,0x1000000,count=count or self.budget)
        assert self.u.reg_read(UC_MIPS_REG_PC)==0x1000000, 'Instruction limit reached'
        assert self.u.reg_read(UC_MIPS_REG_SP)==0x7000f000
        assert [self.u.reg_read(r) for r in SAVED]==[0x12340000+i for i in range(len(SAVED))]
        return signed(self.u.reg_read(UC_MIPS_REG_V0))
    def advance(self,ms,clear=True):
        """Run due UI timers deterministically, including the exact deadline; RET_REPEAT re-arms."""
        if clear: self.calls=[]
        end=self.now+ms
        while self.timers:
            tid,(due,callback,ctx,period)=min(self.timers.items(),key=lambda item:item[1][0])
            if due>end: break
            self.now=due
            del self.timers[tid]
            info=self.alloc(0x58)
            self.word(info+0x20,ctx); self.word(info+0x28,tid)
            ret=self.call(address=callback,args=(info,0,0,0),gap=0,clear=False)
            assert ret in (0,8) and (ret==0 or period>0)
            if ret==8 and tid not in self.timers: self.timers[tid]=(due+period,callback,ctx,period)
        self.now=end
        for a,(start,duration,w,origin,goal) in list(self.slides.items()):
            elapsed=min(end-start,duration)
            self.word(w+O['SLIDE_OFFSET'],round(origin+(goal-origin)*elapsed/duration))
            self.word(a+O['ANIM_ELAPSED'],elapsed)
            if elapsed==duration:
                del self.slides[a]
                callback,ctx=self.slide_callbacks.pop(a)
                assert self.call(address=callback,args=(ctx,0,0,0),gap=0,clear=False)==7
    def confirm(self):
        """Single centre release followed by its full confirmation delay."""
        ret=self.call(O['KEY_CENTER'])
        self.advance(200,clear=False)
        return ret
    def release(self,gap=0):
        """Execute both the hook and the real stock downstream screen-toggle handler."""
        ret=self.call(O['KEY_CENTER'],gap=gap)
        if ret==0:
            self.call(O['KEY_CENTER'],address=syms['on_wm_keyup_fun'],gap=0,clear=False)
        return ret
    def page(self,name='sysset_page',t='scroll_view'):
        child=self.node(t)
        self.top=self.node('window',name,[child])
        if t=='slide_menu':
            self.word(child+O['SLIDE_INDEX'],0)
            self.nodes[child]['children']=[self.entry(child) for _ in range(7)]
            self.word(child+0x5c,self.alloc()) # stock completion checks the child array
        return child
    def page_list(self,n=10,height=96,extent=960,name='sysset_page'):
        """A page holding one scroll view of n 48px entries; returns (surface, entries)."""
        w=self.page(name); self.word(w+O['W_H'],height); self.word(w+O['VIEW_CONTENT_H'],extent)
        es=[self.entry(w,i*48) for i in range(n)]
        self.nodes[w]['children']=es
        return w,es
    def bind(self,rows,offset=0):
        """Reindex a recycled row pool the way the stock table rebind does."""
        for j,r in enumerate(rows):
            self.word(r+O['ROW_INDEX'],offset//48+j); self.word(r+O['W_Y'],(offset//48+j)*48)
    def table_page(self,n=4,name='allmusic_page',rebind=False):
        """A table_client page of n recycled rows, optionally rebound; returns (surface, rows, entries)."""
        w=self.page(name,'table_client')
        self.word(w+O['ROW_HEIGHT'],48); self.word(w+O['TABLE_ROWS'],20); self.word(w+O['W_H'],96)
        rs=[self.node('table_row') for _ in range(n)]
        es=[self.entry(r) for r in rs]; self.nodes[w]['children']=rs
        for r,e in zip(rs,es):
            self.nodes[r]['children']=[e]; self.word(r+O['W_PARENT'],w)
        self.bind(rs)
        if rebind: self.rebind=lambda a,offset: self.bind(rs,offset)
        return w,rs,es
    def list_surface(self,virtual,n=20,rebind=True):
        """A plain list of n 48px rows or a four-row virtual table; optionally rebound."""
        if not virtual:
            w,es=self.page_list(n,extent=n*48)
            return w,None
        w,rs,es=self.table_page(rebind=rebind)
        return w,rs
    def moved(self): return [x for x in self.calls if x[0] in ('scroll_view_set_offset','table_client_set_yoffset','scroll_view_scroll_delta_to','table_client_scroll_to','slide_menu_scroll_to_next','slide_menu_scroll_to_prev','widget_animator_scroll_set_params')]
    def dispatched(self): return [x for x in self.calls if x[0]=='stock_dispatch']

checks=0
def passed():
    global checks
    checks+=1

# Slider settings: wheel ticks dispatch stock +/- clicks rather than changing playback volume.
for page in ('backlight_page','maxvol_page','bootvol_page','balance_page'):
    for key,button in ((O['KEY_NEXT'],'img_add'),(O['KEY_PREV'],'img_dec')):
        m=Machine(); m.page(page); target=m.node('image',button); m.nodes[m.top]['children'].append(target)
        assert m.call(key)==11 and m.clicks==[target]; passed()
        for field in ('animating','pressed'):
            setattr(m,field,1); assert m.call(key)==11 and not m.dispatched(); setattr(m,field,0)
        m.nodes[target]['enable']=0; assert m.call(key)==11 and not m.dispatched(); passed()

# Execute the stock max-volume button callback and Quick Settings brightness callback. Only
# widget storage/event delivery and config/hardware boundaries are mocked.
class SliderSettingsMachine(Machine):
    def __init__(self,page):
        super().__init__(); self.page(page)
        self.mock('write_int_config','config_lightness','slider_set_value','snprintf@GLIBC_2.0')
    def hook(self,u,address,size,unused):
        name=self.handlers.get(address,'')
        if name=='stock_dispatch' and self.nodes[self.top]['name']=='maxvol_page':
            target,event=[u.reg_read(r) for r in REGS[:2]]; self.calls.append((name,target,event,0))
            self.clicks.append(target)
            callback=0x4b783c if self.nodes[target]['name']=='img_add' else 0x4b79d0
            u.reg_write(UC_MIPS_REG_A0,self.top); u.reg_write(UC_MIPS_REG_T9,callback); u.reg_write(UC_MIPS_REG_PC,callback)
            return
        if name=='widget_set_prop_int' and self.nodes[self.top]['name']=='statusbar_dialog':
            target,prop,value=[u.reg_read(r) for r in REGS[:3]]
            if self.text(prop)=='value':
                self.nodes[target]['value']=signed(value)
                event=self.alloc(0x30); self.word(event,O['EVT_VALUE_CHANGED']); self.word(event+0x10,target)
                u.reg_write(UC_MIPS_REG_A0,self.top); u.reg_write(UC_MIPS_REG_A1,event)
                u.reg_write(UC_MIPS_REG_T9,0x49ff54); u.reg_write(UC_MIPS_REG_PC,0x49ff54)
                return
        return super().hook(u,address,size,unused)

m=SliderSettingsMachine('maxvol_page'); m.byte(syms['g_maxvolume'],50)
for name in ('img_add','img_dec','slider_maxvol','label_vol'):
    m.nodes[m.top]['children'].append(m.node('slider' if name.startswith('slider') else 'image',name))
assert m.call(O['KEY_NEXT'])==11 and m.u.mem_read(syms['g_maxvolume'],1)==b'3'
assert [c[1] for c in m.calls if c[0]=='write_int_config']==[51]
assert m.call(O['KEY_PREV'])==11 and m.u.mem_read(syms['g_maxvolume'],1)==b'2'
assert [c[1] for c in m.calls if c[0]=='write_int_config']==[50]; passed()
for value,key,want in ((40,O['KEY_NEXT'],41),(40,O['KEY_PREV'],39),(100,O['KEY_NEXT'],100),(0,O['KEY_PREV'],0)):
    m=SliderSettingsMachine('statusbar_dialog'); slider=m.node('slider','slider_backlight',value=value,min=0,max=100)
    m.nodes[m.top]['children'].append(slider)
    assert m.call(key)==11 and m.nodes[slider]['value']==want
    assert [c[1] for c in m.calls if c[0]=='config_lightness']==([want] if value!=want else []); passed()

# Execute the stock offset setter after cancelling an animator headed to either boundary.
# Cover selected rows and the pixel-scroll fallback.
for populated in (False,True):
    for endpoint,key,selected,want in ((0,O['KEY_NEXT'],4,204),
                                      (864,O['KEY_PREV'],5,180)):
        m=Machine(); w,es=m.page_list(20 if populated else 0)
        m.word(w+0x74,syms['g_scroll_view_vtable'])
        m.paint(w)
        m.nodes[w]['_ringnav_index']=selected if populated else -1
        top=120 if endpoint==0 else 240
        m.word(w+O['SCROLL_Y'],top)
        animator=m.alloc(0x80); m.word(animator+0x64,endpoint)
        m.word(w+O['VIEW_ANIMATOR'],animator)
        del m.handlers[syms['scroll_view_set_offset']]
        m.mock('widget_animator_scroll_create','widget_animator_on',
               'widget_animator_start','widget_dispatch_simple_event')
        assert m.call(key)==11
        active=m.get(w+O['VIEW_ANIMATOR'])
        expected=want if populated else top+(48 if endpoint==0 else -48)
        assert active==0 and m.get(w+O['SCROLL_Y'])==expected
        assert any(c[0]=='widget_animator_destroy' and c[1]==animator for c in m.calls)
        passed()

for t,off in [('scroll_view',O['SCROLL_Y']),('table_client',O['TABLE_TOP'])]:
    m=Machine(); w=m.page(t=t)
    for _ in range(20): assert m.call()==11
    assert m.get(w+off)==min(20*manifest['ring_step_pixels'],720 if t=='scroll_view' else 4560)
    assert m.call(O['KEY_PREV'])==11 and m.moved()
    for _ in range(120): m.call(O['KEY_PREV'])
    assert m.get(w+off)==0
    # Empty/short lists consume input without turning into volume changes.
    m.word(w+(O['VIEW_CONTENT_H'] if t=='scroll_view' else O['TABLE_ROWS']),0)
    assert m.call()==11 and m.get(w+off)==0
    passed()

for name in ['playing_page','volume_dialog','saverscreen_page','usbmode_page','unknown_page']:
    m=Machine(); m.page(name); assert m.call()==0 and not m.moved(); passed()
GATES=[('g_backlight_status',0),('g_lockscreen_pageflag',1),('g_testmode_flag',1),
       ('g_guideflag',1),('g_poweroff_state',2),('g_usblink_status',2),('bt__recv_pageflag',1)]
for flag,value in GATES:
    m=Machine(); m.page(); m.byte(syms[flag],value); assert m.call()==0 and not m.moved(); passed()
# On the active lock screen, two centre releases close the page through its stock Back path. The
# saved key-lock setting is untouched, and another key cancels a pending first tap.
m=Machine(); m.page('poweroff_page'); m.byte(syms['g_lockscreen_pageflag'],1)
assert m.call(O['KEY_CENTER'],gap=0)==11 and not [c for c in m.calls if c[0]=='navigator_back']
assert m.call(O['KEY_CENTER'],gap=100)==11 and [c[0] for c in m.calls].count('navigator_back')==1
assert m.u.mem_read(syms['g_lockscreen_pageflag'],1)==b'\x01'; passed()
m=Machine(); m.page('poweroff_page'); m.byte(syms['g_lockscreen_pageflag'],1)
m.call(O['KEY_CENTER'],gap=0); m.call(O['KEY_PLAY'],gap=50); m.call(O['KEY_CENTER'],gap=50)
assert not [c for c in m.calls if c[0]=='navigator_back']; passed()
# Missing/invalid settings require two clicks; SINGLEWAKE=1 wakes and unlocks with one.
for config,single in (({},False),({'SINGLEWAKE':'1'},True),({'SINGLEWAKE':'0'},False),({'SINGLEWAKE':'bad'},False)):
    for locked in (False,True):
        m=Machine(); m.config=config; m.page('poweroff_page' if locked else 'playing_page')
        m.byte(syms['g_backlight_status'],0); m.byte(syms['g_lockscreen_pageflag'],int(locked))
        assert m.release()==11
        assert m.screens==([1] if single else [])
        assert sum(c[0]=='navigator_back' for c in m.calls)==int(single and locked)
        if not single:
            assert m.release(100)==11 and m.screens==[1]
            assert sum(c[0]=='navigator_back' for c in m.calls)==int(locked)
        assert not m.clicks; passed()
m=Machine(); m.page('playing_page'); m.byte(syms['g_backlight_status'],0)
assert m.release()==11 and m.release(DC+1)==11 and not m.screens
assert m.release(100)==11 and m.screens==[1]; passed()
for field in ['animating','pressed']:
    m=Machine(); m.page(); setattr(m,field,1); assert m.call()==11 and not m.moved(); passed()

m=Machine(); w=m.page('home_page','slide_menu')
assert m.call()==11 and m.get(m.get(w+O['SLIDE_ANIMATOR'])+O['ANIM_X_TO'])==(-240&0xffffffff)
assert m.call(O['KEY_PREV'])==11 and len(m.slides)==1; passed()
m=Machine(); hidden=m.node(visible=0); shown=m.node(); pages=m.node('pages',children=[hidden,shown],active=1)
m.top=m.node('window','artistinfo_page',[pages]); assert m.call()==11 and m.moved()[0][1]==shown; passed()
m.nodes[hidden]['visible']=1
assert m.call()==11 and m.moved()[0][1]==shown; passed()
m.top=m.node('window','sysset_page',[hidden,shown]); assert m.call()==11 and m.moved()[-1][1]==hidden; passed()
# Run the stock setters so incorrect shared offsets cannot make the mocks agree with a bug.
for setter,field in (('scroll_view_set_xslidable','VIEW_HORIZONTAL'),
                     ('scroll_view_set_yslidable','VIEW_VERTICAL'),
                     ('scroll_view_set_snap_to_page','VIEW_SNAP')):
    m=Machine(); vert=m.node(); excluded=m.node()
    m.word(excluded+0x74,syms['g_scroll_view_vtable'])
    value=0 if field=='VIEW_VERTICAL' else 1
    assert m.call(address=syms[setter],args=(excluded,value,0,0))==0
    assert m.u.mem_read(excluded+O[field],1)==bytes([value])
    m.top=m.node('window','sysset_page',[excluded,vert])
    assert m.call()==11 and m.moved()[0][1]==vert
    passed()
# Two navigable panes: only the pane that already holds the selection is used.
m=Machine(); a,b,_,_=m.panes(96)
m.nodes[a]['_ringnav_index']=0
assert m.call()==11 and m.call()==11
assert m.moved()[-1][1]==a
m.nodes[b]['_ringnav_index']=0
assert m.call()==11 and not m.moved(); passed()
for attribute in ['visible','enable']:
    m=Machine(); w=m.page(); m.nodes[w][attribute]=0; assert m.call()==11 and not m.moved(); passed()
# Wheel scrolling sets the offset immediately from the current viewport.
m=Machine(); w=m.page(); m.word(w+O['SCROLL_Y'],100)
assert m.call()==11 and m.moved()[0][0]=='scroll_view_set_offset' and m.moved()[0][3]==148
assert m.get(w+O['SCROLL_Y'])==148 and m.get(w+O['VIEW_ANIMATOR'])==0; passed()

# Painting establishes selection without a sacrificial button press or native focus.
m=Machine(); w,entries=m.page_list(5,extent=1000)
assert m.paint(w)==0 and m.selected(w)==0
if variant=='stock':
    # Default outline: translucent fill, one dark shade stroke, one white stroke, all clipped.
    assert [r['kind'] for r in m.rounded]==['fill','stroke','stroke']
    fill,shade,white=m.rounded
    assert fill['rect']==(1,1,238,46) and fill['bg']==0 and fill['clip']==(0,0,240,96)
    assert fill['color']==FILL and fill['radius']==O['RADIUS'] and fill['width'] is None
    assert shade['rect']==(1,1,238,46) and shade['bg']==0
    assert shade['color']==SHADE
    assert shade['radius']==O['RADIUS'] and shade['width']==1 and shade['clip']==(0,0,240,96)
    assert white['rect']==(2,2,236,44) and white['bg']==0 and white['color']==OUTLINE
    assert white['radius']==O['RADIUS']-1 and white['width']==1
    assert not m.strokes and m.global_alpha==0
    assert m.clip==(0,0,240,240)
    assert m.lcd_colors()==LCD_COLORS
    assert not any(m.get(e+O['W_FOCUS'])&0x80 for e in entries)
    assert [c[0] for c in m.calls if c[0].startswith('canvas_')][-6:]==[
        'canvas_fill_rounded_rect','canvas_stroke_rounded_rect','canvas_stroke_rounded_rect',
        'canvas_set_fill_color','canvas_set_stroke_color','canvas_set_clip_rect']; passed()
else:
    # The bar is painted behind the rows: every band precedes the border hook, which draws nothing.
    names=[c[0] for c in m.calls]
    bg,border=names.index('stock_paint_bg'),names.index('stock_paint')
    assert all(bg<i<border for i,n in enumerate(names) if n=='canvas_fill_rect')
    assert not m.rounded and not m.strokes and m.global_alpha==0
    # Full surface width, one band per row pixel of Graphite's solid fill, then its highlight.
    assert [b[:4] for b in m.bands]==[(0,y,240,1) for y in range(48)]+[(0,0,240,1)]
    assert [m.bands[i][4] for i in (0,47,48)]==[color_t(ACCENTS[0][i]) for i in (0,1,4)]
    assert all(b[5]==(0,0,240,96) for b in m.bands)
    assert m.clip==(0,0,240,240) and m.lcd_colors()==LCD_COLORS
    assert not any(m.get(e+O['W_FOCUS'])&0x80 for e in entries); passed()
    # A grid tile (under half the surface width) gets the bar in its own rect.
    g=Machine(); gw=g.page(); tiles=[g.entry(gw,0) for _ in range(3)]; g.nodes[gw]['children']=tiles
    for i,t in enumerate(tiles): g.word(t+O['W_X'],80*i); g.word(t+O['W_W'],80)
    g.paint(gw); g.call(); g.paint(gw)
    assert g.selected(gw)==1 and g.sel()==(80,0,80,48); passed()
    # Touch mode draws nothing; the wheel brings the bar back.
    c=Machine(); cw,_=c.page_list(5,extent=1000); c.paint(cw)
    c.touch(); c.paint(cw); assert not c.drawn() and c.clip==(0,0,240,240)
    c.call(); c.paint(cw); assert c.selected(cw)==1 and c.sel()==(0,36,240,48); passed()
    # Status bar: a solid darker fill on the bar widget alone, no highlight, LCD fill restored.
    s=Machine(); title=s.node('hscroll_label','label_clock')
    def top_level(t,name,children=()):
        w=s.node(t,name,children); s.word(w+O['W_PARENT'],s.wm); return w
    bar=top_level('system_bar','system_bar',[title]); s.word(bar+O['W_W'],375); s.word(bar+O['W_H'],30)
    s.word(syms['system_bar'],bar)
    def bg(w): return s.call(address=IPOD_HOOKS['widget_on_paint_background'][0],args=(w,s.canvas,0,0))
    assert bg(bar)==0 and [b[:5] for b in s.bands]==[(0,0,375,30,color_t(O['BAR_COLOR']))]
    assert s.lcd_colors()==LCD_COLORS; passed()
    # The clock: local time as 12-hour h:mm AM/PM, written only when the minute shown changes, on
    # the bar's own repaint (stock repaints it each second) or the top window's, whatever the page.
    def clock(): return s.nodes[title].get('text')
    def writes(): return [c for c in s.calls if c[0]=='widget_set_text_utf8']
    assert clock()=='6:14 PM'; passed()  # written by the gradient check's paint above
    for h,mi,want in ((0,0,'12:00 AM'),(0,5,'12:05 AM'),(9,7,'9:07 AM'),(11,59,'11:59 AM'),(12,0,'12:00 PM'),
                      (12,59,'12:59 PM'),(13,0,'1:00 PM'),(23,59,'11:59 PM')):
        s.clock=(h,mi); bg(bar); assert clock()==want,(h,mi,clock())
    passed()
    # Repaints within the minute leave the label alone; the next minute rewrites it.
    bg(bar); assert not writes(); passed()
    s.clock=(0,0); bg(bar); assert clock()=='12:00 AM' and len(writes())==1; passed()
    # A failed time or localtime shows --:-- once; the clock comes back when it reads again.
    for fail in ('time','localtime'):
        s.clock=fail; bg(bar); assert clock()=='--:--' and len(writes())==1
        bg(bar); assert not writes()
        s.clock=(7,30); bg(bar); assert clock()=='7:30 AM'
    passed()
    # Pages, a dialog on top and pages with their own navbar all keep the clock; only a minute change
    # writes it, from any top-level paint.
    heading=s.node('hscroll_label',text='System Setting')
    nav=s.node('view','view_navbar',[s.node('image','img_return'),heading],visible=0)
    pages=[top_level('window',n,[nav] if n=='sysset_page' else []) for n in
           ('sysset_page','home_page','playing_page','coverflow_page','tidal_main_page')]+[top_level('dialog','sortselect_dialog')]
    for pg in pages:
        s.top=pg; bg(pg)
        assert clock()=='7:30 AM' and not writes()
    passed()
    s.clock=(19,31); s.top=pages[-1]; bg(pages[-1]); assert clock()=='7:31 PM' and len(writes())==1; passed()
    # The codec badge and the Battery setting (bar_sync, on each bar paint): stock sets img_bt's image
    # each second; a new badge shows CODEC_MS, fades out and BT_GLYPH fades in over CODEC_STEPS steps
    # each way, and BT_GLYPH then replaces the badge stock sets again. The battery shows the icon, the
    # percentage or view_battery, and the icon while the group's ink would reach past BATT_ROOM.
    def battery_checks():
        def battery_bar(mode):
            b=Machine(); b.config={'BATTERY':str(mode)}
            level=b.node('progress_bar','progress_battery',value=50)
            w={n:b.node(t,n,list(c),visible=v,**kw) for n,t,c,v,kw in (
                ('img_bt','image',(),1,dict(image='bar_ldac')),('img_wifi','image',(),1,{}),
                ('label_battery','label',(),0,{}),('view_battery','view',(),0,{}),
                ('img_battery','image',(level,),1,dict(image='bar_battery')))}
            b.word(w['img_wifi']+O['W_W'],16); b.word(w['view_battery']+O['W_H'],30)
            bar=b.node('system_bar','system_bar',[b.node('view','view_right',list(w.values()))])
            w['progress_battery']=level
            b.word(bar+O['W_PARENT'],b.wm); b.word(syms['system_bar'],bar)
            return b,bar,w,lambda x=bar: b.call(address=IPOD_HOOKS['widget_on_paint_background'][0],args=(x,b.canvas,0,0),gap=0)
        def shown(b,w): return [n for n in ('img_battery','label_battery','view_battery') if b.nodes[w[n]]['visible']]
        def alpha(b,w): return b.get(w['img_bt']+0x34)&255
        b,bar,w,paint=battery_bar(1); paint()
        # LDAC's badge (36px of ink) and Wi-Fi leave no room for the percentage: the icon, until it fades.
        assert b.nodes[w['img_bt']]['image']=='bar_ldac' and alpha(b,w)==255 and shown(b,w)==['img_battery'] and len(b.timers)==1
        b.advance(O['CODEC_MS']-1); paint(); assert b.nodes[w['img_bt']]['image']=='bar_ldac' and alpha(b,w)==255
        steps=[]
        for _ in range(2*O['CODEC_STEPS']):
            b.advance(O['CODEC_STEP_MS'] if steps else 1); steps.append((alpha(b,w),b.nodes[w['img_bt']]['image']))
        n=O['CODEC_STEPS']
        assert steps==[(255*abs(n-k)//n,'bar_ldac' if k<n else O_GLYPH) for k in range(1,2*n+1)] and not b.timers, steps
        paint(); assert shown(b,w)==['label_battery']; passed()
        # Stock's next tick sets the badge again: the glyph goes straight back, with no new fade.
        b.nodes[w['img_bt']]['image']='bar_ldac'; paint()
        assert b.nodes[w['img_bt']]['image']==O_GLYPH and not b.timers and shown(b,w)==['label_battery']; passed()
        # A new codec flashes again; AAC's narrow badge leaves room, so the percentage stays.
        b.nodes[w['img_bt']]['image']='bar_aac'; paint()
        assert b.nodes[w['img_bt']]['image']=='bar_aac' and len(b.timers)==1 and shown(b,w)==['label_battery']; passed()
        # Bluetooth off ends the flash; a reconnect with the same codec flashes again.
        b.nodes[w['img_bt']]['visible']=0; paint(); assert not b.timers and alpha(b,w)==255
        b.nodes[w['img_bt']]['visible']=1; paint(); assert len(b.timers)==1; passed()
        # Icon + Percent: the outline, nub and level in one colour, canvas state restored; charging is
        # BATT_CHARGE_RGB, low the accent's red tone; the slot repaints only when one of them changes.
        b,bar,w,paint=battery_bar(2); b.nodes[w['img_bt']]['visible']=0
        lcd=lambda: (b.lcd_colors(),b.get(b.lcd+O['LCD_TEXT_COLOR']),b.get(b.canvas+O['CANVAS_ALIGN_V']),b.get(b.canvas+O['CANVAS_ALIGN_H']))
        paint(); assert shown(b,w)==['view_battery']; before=lcd()
        def slot(): b.bands=[]; b.letters=[]; paint(w['view_battery']); assert lcd()==before; return b.bands,b.letters
        bw,bh,y=O['BATT_BODY_W'],O['BATT_BODY_H'],(30+1-O['BATT_BODY_H'])//2
        for image,value,rgb,text in (('bar_battery',88,0xffffff,'88'),('bar_charge',63,O['BATT_CHARGE_RGB'],'63'),('bar_charge',100,O['BATT_CHARGE_RGB'],'100'),
                                     ('bar_lowcharge',5,ACCENTS[0][3],'5')):
            # the level is label_battery's text: stock zeroes progress_battery while charging
            b.nodes[w['img_battery']]['image']=image; b.nodes[w['label_battery']]['text']=f'{value}%'
            b.nodes[w['progress_battery']]['value']=0 if image=='bar_charge' else value
            b.calls=[]; paint(); assert ('widget_invalidate_force',w['view_battery']) in [c[:2] for c in b.calls]
            b.calls=[]; paint(); assert ('widget_invalidate_force',w['view_battery']) not in [c[:2] for c in b.calls]
            bands,letters=slot()
            assert [x[:5] for x in bands]==[(1,y,bw-2,1,color_t(rgb)),(1,y+bh-1,bw-2,1,color_t(rgb)),(0,y+1,1,bh-2,color_t(rgb)),
                                             (bw-1,y+1,1,bh-2,color_t(rgb)),(bw,y+(bh-O['BATT_NUB_H'])//2,O['BATT_NUB_W'],O['BATT_NUB_H'],color_t(rgb))]
            assert [(l['text'],l['rect'],l['color'],l['font'],l['align']) for l in letters]==[(text,(0,y+1,bw,bh),color_t(rgb),('default',O['BATT_PX']),(1,1))]
        passed()
    battery_checks()
assert m.confirm()==11 and m.dispatched()[0][1]==entries[0]; passed()
assert m.call()==11 and m.selected(w)==1 and m.get(w+O['SCROLL_Y'])==12
assert m.call()==11 and m.selected(w)==2 and m.get(w+O['SCROLL_Y'])==60
assert m.confirm()==11 and m.dispatched()[0][1]==entries[2]; passed()
# The separate Play/Pause key remains native even with an active selection.
assert m.call(O['KEY_PLAY'])==0 and not m.dispatched(); passed()
# Touching a different row selects it before the native callback runs; no extra click.
assert m.touch()==0
m.on_click=lambda a,b: (None if m.selected(w)==1 else (_ for _ in ()).throw(AssertionError('late selection')))
assert m.click(entries[1])==0 and len(m.dispatched())==1 and m.selected(w)==1
m.on_click=None
assert m.confirm()==11 and m.dispatched()[0][1]==entries[1]; passed()
# Native touch focus may move anywhere without altering the logical selection.
m.word(entries[4]+O['W_FOCUS'],0x80)
assert m.confirm()==11 and m.dispatched()[0][1]==entries[1]; passed()
# Swipe preserves selection during momentum; settle adopts the visible row nearest the centre.
m.touch(); m.word(w+O['SCROLL_Y'],110); m.word(w+O['VIEW_ANIMATOR'],0x1234)
m.paint(w); assert m.selected(w)==1
m.word(w+O['VIEW_ANIMATOR'],0); m.paint(w); assert m.selected(w)==3
assert m.confirm()==11 and m.dispatched()[0][1]==entries[3]; passed()
# Wheel interrupts touch momentum, and centre while a finger is down is consumed without a click.
m.touch(); m.word(w+O['VIEW_ANIMATOR'],0x1234)
assert m.call(O['KEY_PREV'])==11 and m.get(w+O['VIEW_ANIMATOR'])==0 and m.selected(w)==2
m.pressed=1
assert m.confirm()==11 and not m.dispatched()
assert m.call()==11 and m.selected(w)==2
m.pressed=0; passed()
# With three rows in view the difference shows: the middle one wins, not the top edge.
m=Machine(); w,es=m.page_list(8,height=192,extent=1000)
m.paint(w); m.touch(); m.word(w+O['SCROLL_Y'],200); m.word(w+O['VIEW_ANIMATOR'],0x1234)
m.paint(w); assert m.selected(w)==0
m.word(w+O['VIEW_ANIMATOR'],0); m.paint(w); assert m.selected(w)==6; passed()
# Native click may destroy the current page. Nothing dereferences its target afterwards.
def destroy(a,b):
    m.nodes.clear(); m.top=0
m.on_click=destroy
assert m.confirm()==11 and len(m.dispatched())==1; passed()

# Execute native row-pool constructors, including the untouched album grid branch.
# The 52px artwork keeps the stock nine-pixel inset in Stock; iPod keeps that same 52px
# artwork at natural size (no rescaling) and gives it an even eight-pixel inset on all four
# sides of the 68px row body. The playing overlay follows.
ARTWORK = {
    0x523038: [('img_icon', (4, 9, 52, 52), (0, 8, 52, 52)),
               ('git_playing', (0, 9, 52, 52), (0, 8, 52, 52))],
    0x4aa2cc: [('img_icon', (48, 0, 50, 70), (48, 0, 52, 68)),
               ('img_gifbg', (0, 9, 50, 52), (0, 8, 50, 52))],
    0x4b0efc: [('img_icon', (48, 0, 50, 70), (48, 0, 52, 68))],
    0x4a4ae8: [('img_icon', (48, 0, 50, 70), (48, 0, 52, 68)),
               ('img_gifbg', (0, 9, 50, 52), (0, 8, 50, 52))],
}

def native_row_layout(m, button):
    # Run the real stock layout dispatcher and horizontal layouter. Mock only toolkit
    # collection/geometry services, not the width arithmetic or native child positioning.
    m.handlers.pop(syms['widget_layout_children'],None)
    m.mock('widget_vtable_on_layout_children', 'widget_layout_self',
           'widget_layout_floating_children', 'widget_get_children_for_layout',
           'darray_init', 'darray_deinit', 'widget_move_resize_ex')
    for node, props in m.nodes.items():
        children=props['children']
        if children:
            array=m.alloc(12); values=m.alloc(4*len(children))
            for i,child in enumerate(children): m.word(values+4*i,child)
            m.word(array,len(children)); m.word(array+8,values); m.word(node+0x5c,array)
    assert m.call(address=syms['widget_layout_children'],args=(button,0,0,0))==0

def check_title_bounds(m, button, drill=False):
    children=m.nodes[button]['children']
    text=next(c for c in children if m.nodes[c]['type']=='hscroll_label' or any(
        m.nodes[t]['type']=='hscroll_label' for t in m.nodes[c]['children']))
    title=next((c for c in m.nodes[text]['children'] if m.nodes[c]['type']=='hscroll_label'),text)
    layout=m.get(button+O['W_CHILDREN_LAYOUT'])
    vtable=m.get(layout+O['CHILDREN_LAYOUT_VTABLE'])
    stock_vtable=O['DEFAULT_LAYOUT_VTABLE']
    assert (vtable!=stock_vtable)==(variant=='ipod')
    # Clone, destroy, parameter and serialization functions remain native.
    for i in (0,1,3,4,5,6,7): assert m.get(vtable+4*i)==m.get(stock_vtable+4*i)
    margin=m.u.mem_read(layout+O['DEFAULT_LAYOUT_X_MARGIN'],1)[0]
    gap=m.u.mem_read(layout+O['DEFAULT_LAYOUT_SPACING'],1)[0]
    widths={c:m.get(c+O['W_W']) for c in children}
    title_width=m.get(title+O['W_W'])
    title_yh=(m.get(title+O['W_Y']),m.get(title+O['W_H']))
    attributes={k:v for k,v in m.nodes[title].items() if k=='style' or 'hscroll' in k}
    for artwork in (0,1):
        for controls in (0,1):
            for choice in (0,1):
                for c in children:
                    if c==text: continue
                    name=m.nodes[c]['name']
                    m.nodes[c]['visible']=choice if name.startswith('img_choice') else controls if c in children[children.index(text)+1:] else artwork
                # Rebind the same row with different strings, then repeat layout (scroll/reuse).
                for label in ('Short','A very long title '*12,'日本語の長い曲名と歌手 — සිංහල — العربية'):
                    m.nodes[title]['text']=label
                    native_row_layout(m,button)
                    before=children[:children.index(text)]
                    after=children[children.index(text)+1:]
                    left=margin+sum(widths[c]+gap for c in before if m.nodes[c]['visible'])
                    # A drill row lays out as if a stock img_into (CHEVRON_W with the margin) came last.
                    edge=O['CHEVRON_W']+gap if drill and variant=='ipod' else margin
                    right=m.get(button+O['W_W'])-edge-sum(widths[c]+gap for c in after if m.nodes[c]['visible'])
                    assert m.get(text+O['W_X'])==left
                    if variant=='ipod':
                        assert left+m.get(text+O['W_W'])==right
                        if title!=text:
                            assert m.get(title+O['W_X'])+m.get(title+O['W_W'])==m.get(text+O['W_W'])
                        cursor=right+gap
                        for c in after:
                            if m.nodes[c]['visible']:
                                assert m.get(c+O['W_X'])==cursor
                                cursor+=widths[c]+gap
                        assert cursor-gap==m.get(button+O['W_W'])-edge
                    else:
                        assert m.get(text+O['W_W'])==widths[text] and m.get(title+O['W_W'])==title_width
                    assert (m.get(title+O['W_Y']),m.get(title+O['W_H']))==title_yh
                    assert m.nodes[title]['text']==label
                    assert all(m.nodes[title][k]==v for k,v in attributes.items())
                    for c in children:
                        if c!=text: assert m.get(c+O['W_W'])==widths[c]

for address in (0x523038, 0x4aa2cc, 0x4b0efc, 0x4a4ae8):
    for grid in ((0, 1) if address == 0x4a4ae8 else (0,)):
        m = Machine(); w = m.page('folder_page', 'table_client')
        m.mock('table_row_create', 'button_create', 'image_create', 'view_create',
               'hscroll_label_create', 'gif_image_create', 'widget_use_style',
               'widget_set_name', 'widget_set_children_layout', 'image_set_draw_type',
               'image_base_set_image', 'set_hscroll_label_attribute')
        m.word(syms['album_modetype'], grid)
        assert m.call(address=address, args=(w, w, 4, 0)) == 0
        rows = m.nodes[w]['children']
        assert len(rows) == 4
        for row in rows:
            assert m.get(row+O['W_H']) == (210 if grid else 72 if variant == 'ipod' else 78)
            for button in m.nodes[row]['children']:
                assert m.get(button+O['W_H']) == (160 if grid else 68 if variant == 'ipod' else 70)
        if not grid:
            for name, stock, compact_geometry in ARTWORK[address]:
                arts = [n for n, v in m.nodes.items() if v.get('name') == name]
                assert arts, name
                for art in arts:
                    geometry = tuple(signed(m.get(art+O[off])) for off in ('W_X', 'W_Y', 'W_W', 'W_H'))
                    assert geometry == (compact_geometry if variant == 'ipod' else stock), (name, geometry)
        # Preparing an existing pool does not recreate or resize its rows.
        before = len(m.nodes)
        assert m.call(address=address, args=(w, w, 4, 0)) == 0 and len(m.nodes) == before
        if not grid:
            for row in rows[:2]:  # independently recreated rows as well as repeated reuse
                check_title_bounds(m,m.nodes[row]['children'][0])
            into=[n for n,v in m.nodes.items() if v.get('name')=='img_into']
            if into and variant=='ipod':  # the payload's chevron column is stock's, after layout
                button=m.nodes[rows[0]]['children'][0]
                assert m.get(into[0]+O['W_X'])==m.get(button+O['W_W'])-O['CHEVRON_W'] and m.get(into[0]+O['W_W'])==50
            if address==0x523038:
                # Execute the real folder text/style rebind after layout: its stock 140/190px
                # reset must no longer undo the computed width while a row pool is recycled.
                button=m.nodes[rows[0]]['children'][0]
                title=next(c for c in m.nodes[button]['children'] if m.nodes[c]['name']=='scrlabel_name')
                width=m.get(title+O['W_W'])
                m.row_record=m.alloc(0x80); m.word(m.row_record+0x34,8)
                m.mock('deque_at','tk_snprintf','widget_set_text_utf8','file_is_playing')
                for navbar in (0,1,0):
                    m.byte(syms['g_navbar_status'],navbar)
                    label='再利用された長いフォルダー名 '*5
                    m.word(m.row_record+8,m.string(label))
                    assert m.call(address=0x522304,args=(button,7,0,0))==0
                    assert m.nodes[title]['text']==label
                    assert m.get(title+O['W_W'])==(width if variant=='ipod' else 140 if navbar else 190)
        else:
            assert all(m.get(n+O['W_CHILDREN_LAYOUT'])==0 for row in rows for n in m.nodes[row]['children'])
passed()

# The three non-pooled constructors: album tracks, artist tracks and playlists.
# These use distinct title/container names and must receive the same native layout behavior;
# only a drill window (playlist_page) reserves the chevron's space.
for address,page in ((0x4a62c0,'artistinfo_page'),(0x4adcbc,'artistinfo_page'),(0x4b2864,'artistinfo_page'),(0x4b2864,'playlist_page')):
    for reopen in range(2):
        m=Machine(); w=m.page(page,'scroll_view')
        m.word(w+O['W_PARENT'],m.top); m.word(m.top+O['W_PARENT'],m.wm)
        m.nodes[w]['name']='scroll_view_track'
        m.row_record=m.alloc(0x80)
        for off in (8,12,16,24): m.word(m.row_record+off,m.string('日本語 Title'))
        m.word(m.row_record+0x48,1)
        m.mock('button_create','list_item_create','image_create','view_create','hscroll_label_create',
               'gif_image_create','widget_use_style','widget_set_name','widget_set_text_utf8',
               'widget_set_visible','widget_on','widget_destroy_children','image_set_draw_type',
               'image_base_set_image','set_hscroll_label_attribute','hscroll_label_set_only_focus',
               'hscroll_label_set_ellipses','hscroll_label_set_speed','gif_image_play','deque_at',
               'deque_size','tk_snprintf','batch_get_selectitem','file_is_playing','getFormatString',
               'getMusicByPlayList','get_albumcover_listsize','mclGetPlayStatus','list_get_img_pic',
               'strcmp@GLIBC_2.0','toolsTrimLeft','toolsTrimRight')
        m.handlers[0x4aad10]='row_toolbar'
        assert m.call(address=address,args=(w,0,0,0))==0
        buttons=[n for n,v in m.nodes.items() if v['type']=='button']
        assert len(buttons)==1
        check_title_bounds(m,buttons[0],drill=page=='playlist_page')
passed()

# Long Return executes the stock gates and release filter in both variants.
def long_machine():
    m = Machine()
    m.mock('netdisk_folder_clear', 'navigator_back_to_home', 'awake_screen',
           'getFormatString', 'navigator_to_with_context', 'navigator_window_is_exist')
    return m

def long_return(m):
    return m.call(170, address=syms['on_wm_keylong_fun'], event_type=0x111, gap=0)

def destinations(m):
    return [c for c in m.calls if c[0] in ('navigator_back_to_home', 'navigator_switch_to_with_context')]

for page in ('home_page', 'folder_page', 'playing_page', 'sysset_page'):
    m = long_machine(); m.page(page)
    # No playback data is initialized: the switch must work with an empty queue as well.
    assert long_return(m) == 0
    dest = destinations(m)
    assert len(dest) == 1
    assert dest[0][0] == ('navigator_switch_to_with_context' if variant == 'ipod' and page != 'playing_page' else 'navigator_back_to_home')
    if variant == 'ipod' and page != 'playing_page':
        assert m.text(dest[0][1]) == 'playing_page'
        assert [m.get(dest[0][2] + 4*i) for i in range(4)] == [0, 0, 255, 2]
    # Here the navigator is only recorded, so the switch never lands and the latch is left
    # armed; the NavigationMachine checks below cover the landed switch that drops it.
    held = 11
    assert m.call(170, gap=0) == held
    assert m.call(170, gap=0) == 0   # next short Return still reaches stock Back
    for _ in range(3):
        assert long_return(m) == 0
        assert len(destinations(m)) == 1
    assert m.call(170, gap=0) == held
passed()

# The stock long-key gates stay effective. iPod adds the shared navigation restrictions.
for flag, value in GATES:
    m = long_machine(); m.page('folder_page'); m.byte(syms[flag], value)
    long_return(m)
    # Stock itself blocks power-off, guide and test mode; iPod adds the shared restrictions
    # checked by usable(), so every listed flag blocks there. Stock ignores the rest on Return.
    blocked = True if variant == 'ipod' else flag in ('g_poweroff_state', 'g_guideflag', 'g_testmode_flag')
    assert bool(destinations(m)) == (not blocked), flag
for light in (0, 1):
    for lock in (0, 1):
        for mode in range(4):
            m = long_machine(); m.page('folder_page')
            m.byte(syms['g_backlight_status'], light)
            m.byte(syms['g_keylock_flag'], lock); m.byte(syms['g_keylock_mode'], mode)
            long_return(m)
            blocked = not light and lock and mode in (2, 3)
            if variant == 'ipod': blocked = not light
            assert bool(destinations(m)) == (not blocked)
passed()
if variant == 'ipod':
    m = long_machine(); m.page_list()
    assert m.call(O['KEY_CENTER']) == 11 and m.timers
    long_return(m)
    assert not m.timers
    m.advance(201)
    assert not m.dispatched()
    # A hold blocked elsewhere still swallows its release, so no delayed page action runs.
    m = long_machine(); m.page('folder_page'); m.byte(syms['g_usblink_status'], 2)
    long_return(m)
    assert not destinations(m)
    assert m.call(170, gap=0) == 11
passed()
# Other long-key paths remain stock; exercise the inert keys and power gate.
# Play/Pause is covered below with a real input-device record.
for key in (172, 173, 222, 223, 218):
    m = long_machine(); m.page('home_page')
    if key == 218: m.byte(syms['g_poweroff_state'], 2)
    m.call(key, address=syms['on_wm_keylong_fun'], event_type=0x111)
    assert not destinations(m)
passed()

# Four complete compact rows resolve the same target for touch and centre at every row.
for index in range(4):
    m = Machine(); w, es = m.page_list(4, height=288, extent=288, name='folder_page')
    for i, e in enumerate(es):
        m.word(e+O['W_Y'], i*72); m.word(e+O['W_H'], 68)
    m.touch(); m.click(es[index])
    assert m.confirm() == 11 and m.dispatched()[0][1] == es[index]
passed()

# Recycle a small row pool: selection belongs to the logical index, never the widget.
m=Machine(); w,rows,entries=m.table_page(rebind=True)
m.paint(w); assert m.selected(w)==0
assert m.call()==11 and m.selected(w)==1
assert m.call()==11 and m.selected(w)==2 and m.get(w+O['TABLE_TOP'])==60
assert m.moved()[-1][0]=='table_client_set_yoffset'
assert m.confirm()==11 and m.dispatched()[0][1]==entries[1]; passed()
# A wheel step ends its scroll as a touch scroll does, so stock lists load the covers in view.
m=Machine(); w,rows,entries=m.table_page(rebind=True); m.mock('widget_dispatch_simple_event')
m.paint(w); m.call(); m.call()
assert ('widget_dispatch_simple_event',w,O['EVT_SCROLL_END']) in [c[:3] for c in m.calls]; passed()
# A touch click in a rebound row immediately changes what centre opens.
m.touch(); m.click(entries[0]); assert m.selected(w)==1
assert m.confirm()==11 and m.dispatched()[0][1]==entries[0]; passed()
# Swipe out of the old pool, then centre: settle/re-resolve before dispatch.
m.touch(); m.word(w+O['TABLE_TOP'],480); m.bind(rows,480); m.word(w+O['TABLE_ANIMATOR'],0x9876)
assert m.confirm()==11 and m.selected(w)==10 and m.dispatched()[0][1]==entries[0]
assert m.get(w+O['TABLE_ANIMATOR'])==0; passed()
# Returning to a surviving menu keeps a valid selection; shrinking data repairs it.
oldtop=m.top; m.page('playing_page'); assert m.confirm()==0
m.top=oldtop; m.paint(w); assert m.selected(w)==10
m.word(w+O['TABLE_ROWS'],1); m.word(w+O['TABLE_TOP'],0); m.bind(rows); m.paint(w)
assert m.selected(w)==0; passed()

# A recreated page recalls the last selected row and reveals it without a sacrificial press.
def walk(n,steps,texts=(),extent=1000,**kw):
    """Paint a fresh labelled list of n entries, then wheel down steps rows."""
    m=Machine(); w,es=m.page_list(n,extent=extent,**kw); m.label(es,texts); m.paint(w)
    for _ in range(steps): assert m.call()==11
    assert m.selected(w)==steps
    return m,w,es
m,w,es=walk(10,5); assert m.get(w+O['SCROLL_Y'])==204
w2,es2=m.page_list(10,extent=1000)
assert m.paint(w2)==0 and m.selected(w2)==5 and m.get(w2+O['SCROLL_Y'])==204
assert m.sel()==(0,36,240,48)
assert m.confirm()==11 and m.dispatched()[0][1]==es2[5]; passed()

# Memory is per audited context: visiting another page leaves it alone.
w3,es3=m.page_list(10,extent=1000,name='display_page')
assert m.paint(w3)==0 and m.selected(w3)==0
w4,es4=m.page_list(10,extent=1000)
assert m.paint(w4)==0 and m.selected(w4)==5; passed()

# A stale remembered row is ignored when the new list is shorter.
w5,es5=m.page_list(2,extent=300)
assert m.paint(w5)==0 and m.selected(w5)==0
assert m.confirm()==11 and m.dispatched()[0][1]==es5[0]; passed()

# A settled swipe stores the row the user sees, not the pre-swipe selection.
m=Machine(); w,es=m.page_list(10,extent=1000)
m.paint(w); m.touch(); m.word(w+O['SCROLL_Y'],240); m.word(w+O['VIEW_ANIMATOR'],0x1234)
m.paint(w); assert m.selected(w)==0
m.word(w+O['VIEW_ANIMATOR'],0); m.paint(w); assert m.selected(w)==5
w2,es2=m.page_list(10,extent=1000)
assert m.paint(w2)==0 and m.selected(w2)==5 and m.get(w2+O['SCROLL_Y'])==204; passed()

# Position memory follows row text across a recreation, not just the index.
m,w,es=walk(6,4,[f'track {i}' for i in range(6)])
w2,es2=m.page_list(6,extent=1000); m.label(es2,[f'track {i}' for i in (4,0,1,2,3,5)])
assert m.paint(w2)==0 and m.selected(w2)==0
assert m.confirm()==11 and m.dispatched()[0][1]==es2[0]; passed()
# The same recall with (rows, detents, texts before, texts after recreation, recalled row).
for n,steps,before,after,want in (
        (6,3,[],[],3),  # rows without text fall back to the remembered index
        # Duplicate row text restores the occurrence the user left, not the first duplicate.
        (6,4,['Intro']*6,['Intro']*6,4),
        # After a re-sort, equal-text rows resolve to the occurrence nearest the old position.
        (6,4,[None]*4+['dup'],[None,'dup',None,None,None,'dup'],5),
        (6,3,[None]*3+['dup'],[None,None,'dup',None,'dup'],2),  # equal distance keeps the earlier
        # Two rows share a title; the second text decides before proximity does.
        (4,1,[('Intro','Artist A'),('Intro','Artist B'),'Intro','Intro'],
             ['Intro','Intro',('Intro','Artist A'),('Intro','Artist B')],3),
        # A subtitle missing from the recreated rows never blocks the primary match.
        (4,1,['Intro',('Intro','Artist B'),'Intro','Intro'],['Intro',None,None,'Intro'],0)):
    m,w,es=walk(n,steps,before)
    w2,es2=m.page_list(n,extent=1000); m.label(es2,after)
    assert m.paint(w2)==0 and m.selected(w2)==want
    passed()

# Run the actual stock label text accessor, including value_wstr and the label vtable.
# U+0100 and U+0200 share a low zero byte; titles/subtitles must use complete code points.
m=Machine(); w,es=m.page_list(4,extent=1000)
del m.handlers[syms['widget_get_text']]
m.handlers[syms['strcmp@GLIBC_2.0']]='tk_strcmp'
def stock_text(e,text):
    m.word(e+0x74,syms['g_label_vtable'])
    m.word(e+0x38,len(text)); m.word(e+0x40,m.wide_string(text))
for i,e in enumerate(es):
    stock_text(e,'\u0100 Intro')
    sub=m.node('label'); stock_text(sub,['\u0100','\u0200','\U0001f600','音楽'][i])
    m.nodes[e]['children']=[sub]
# The narrow accessor really returns NULL, even though widget_get_text sees the label.
del m.handlers[syms['widget_get_prop_str']]
assert m.call(address=syms['widget_get_prop_str'],args=(es[0],m.string('text'),0,0))==0
m.mock('widget_get_prop_str')
m.paint(w); m.call(); assert m.selected(w)==1
w2,es2=m.page_list(4,extent=1000)
for i,e in enumerate(es2):
    stock_text(e,'\u0100 Intro')
    sub=m.node('label'); stock_text(sub,['\u0100','\U0001f600','音楽','\u0200'][i])
    m.nodes[e]['children']=[sub]
assert m.paint(w2)==0 and m.selected(w2)==3
assert m.confirm()==11 and m.dispatched()[0][1]==es2[3]; passed()

# Folder memory belongs to the full path, even when every visible row title is identical.
m=Machine(); m.folder('/sd/Folder A')
w,es=m.page_list(8,name='folder_page'); m.label(es,['Intro']*8)
m.paint(w)
for _ in range(5): m.call()
w2,es2=m.page_list(8,name='folder_page'); m.label(es2,['Intro']*8)
m.paint(w2); assert m.selected(w2)==5  # same path still recalls
m.folder('/sd/Folder B')
w3,es3=m.page_list(8,name='folder_page'); m.label(es3,['Intro']*8)
m.paint(w3); assert m.selected(w3)==0
# A surviving surface rebound to another folder also resets, even at the same row count.
for _ in range(3): m.call()
m.folder('/sd/Folder C')
m.word(w3+O['SCROLL_Y'],0); m.paint(w3); assert m.selected(w3)==0; passed()
# Nested folder returns restore each level on recreated or rebound surfaces.
for reuse in (False,True):
    m=Machine(); w,es=m.page_list(10,name='folder_page')
    for path,wanted in (('/sd',6),('/sd/child',4),('/sd/child/grandchild',2)):
        m.folder(path)
        if not reuse: w,es=m.page_list(10,name='folder_page')
        m.word(w+O['SCROLL_Y'],0); m.paint(w); assert m.selected(w)==0
        for _ in range(wanted): m.call()
    for path,wanted in (('/sd/child',4),('/sd',6),('/sd/child/grandchild',2)):
        m.folder(path)
        if not reuse: w,es=m.page_list(10,name='folder_page')
        m.word(w+O['SCROLL_Y'],0); m.paint(w)
        assert m.selected(w)==wanted and m.get(w+O['SCROLL_Y'])==(wanted-1)*48+12
    passed()

# Exactly 64 scopes fit. Selection promotes; restoration alone does not change recency.
for promote in (False,True):
    m=Machine(); w,es=m.page_list(4,name='folder_page')
    for i in range(64):
        m.folder(f'/sd/{i}',w); m.call(); m.call()
    m.folder('/sd/0',w); assert m.selected(w)==2
    if promote: m.call()  # update and protect the oldest entry
    m.folder('/sd/64',w); m.call()
    for i,wanted in ((0,3),(2,2)) if promote else ((1,2),(63,2)):
        m.folder(f'/sd/{i}',w); assert m.selected(w)==wanted
    evicted=1 if promote else 0
    m.folder(f'/sd/{evicted}',w); assert m.selected(w)==0
    passed()

# A full history must not add work to steady painting or turns in the current scope.
costs=[]
for occupancy in (1,64):
    m=Machine(); w,es=m.page_list(20,height=192,extent=960,name='folder_page')
    for i in range(occupancy):
        m.folder(f'/sd/{i:02}',w); m.call()
    instructions=[0]
    def count_payload(*args): instructions[0]+=1
    hook=m.u.hook_add(UC_HOOK_CODE,count_payload,begin=BASE,end=SCRATCH-1)
    m.paint(w); paint_cost=instructions[0]; instructions[0]=0
    m.call(); costs.append((paint_cost,instructions[0]))
    m.u.hook_del(hook)
assert costs[0]==costs[1],costs
passed()

# Returning after another scope still applies text matching and stale-index rejection.
for shortened in (False,True):
    m=Machine(); m.folder('/sd/parent')
    w,es=m.page_list(8,name='folder_page'); m.label(es,[f'track {i}' for i in range(8)]); m.paint(w)
    for _ in range(6): m.call()
    m.folder('/sd/child')
    w,es=m.page_list(8,name='folder_page'); m.paint(w); m.call()
    m.folder('/sd/parent')
    w,es=m.page_list(3 if shortened else 8,name='folder_page'); m.label(es,[f'track {i}' for i in range(8)])
    if not shortened: m.nodes[es[3]]['text']='track 6'; m.nodes[es[6]]['text']='other'
    m.paint(w); assert m.selected(w)==(0 if shortened else 3)
    passed()

# An unavailable or unterminated path cannot supply a content identity.
for path in (b'\0',b'x'*1024):
    m=Machine(); m.u.mem_write(syms['g_folder_path'],path)
    w,es=m.page_list(8,name='folder_page'); m.paint(w); m.call(); m.call()
    w2,es2=m.page_list(8,name='folder_page'); m.paint(w2)
    assert m.selected(w2)==0; passed()

# Same-sized local lists must not inherit selection across query or browsing-mode changes.
for changed in ('g_class_type','g_local_classinfo_save','g_artist_type','album_modetype'):
    m=Machine(); w,rs,es=m.table_page(); m.paint(w); m.call(); m.call()
    w2,rs2,es2=m.table_page(); m.paint(w2); assert m.selected(w2)==2
    m.word(syms[changed],m.get(syms[changed])+1)
    w3,rs3,es3=m.table_page(); m.paint(w3); assert m.selected(w3)==0
    m.call(); m.call()
    m.word(syms[changed],m.get(syms[changed])+1)
    m.word(w3+O['TABLE_TOP'],0); m.paint(w3); assert m.selected(w3)==0
    passed()
# Each query can be revisited, including on a surface reused for other queries.
for reuse in (False,True):
    for changed in ('g_class_type','g_local_classinfo_save','g_artist_type','album_modetype'):
        m=Machine(); w,rs,es=m.table_page(n=20)
        for query,wanted in ((10,7),(20,4),(30,2)):
            m.word(syms[changed],query)
            if not reuse: w,rs,es=m.table_page(n=20)
            m.word(w+O['TABLE_TOP'],0); m.paint(w); assert m.selected(w)==0
            for _ in range(wanted): m.call()
        for query,wanted in ((10,7),(20,4),(30,2)):
            m.word(syms[changed],query)
            if not reuse: w,rs,es=m.table_page(n=20)
            m.word(w+O['TABLE_TOP'],0); m.paint(w)
            assert m.selected(w)==wanted and m.get(w+O['TABLE_TOP'])==(wanted-1)*48+12
        passed()
# Without an audited content identity, a recreated detail, dialog or network page starts fresh.
for name in ('netdiskfolder_page','tidal_albuminfo_page','playerqueue_page',
             'search_dialog','tidal_search_dialog'):
    m=Machine(); w,es=m.page_list(8,name=name); m.paint(w)
    for _ in range(3): m.call()
    m.paint(w); assert m.selected(w)==3
    w2,es2=m.page_list(8,name=name); m.paint(w2); assert m.selected(w2)==0
    passed()

# Search result dialogs navigate their list and centre opens the highlighted result.
for name in ('search_dialog','tidal_search_dialog'):
    m=Machine(); w,es=m.page_list(8,name=name)
    m.paint(w)
    for _ in range(2): assert m.call()==11
    assert m.selected(w)==2
    assert m.confirm()==11 and m.dispatched()[0][1]==es[2]
    passed()
# The search input dialogs have no navigable pane: they stay off the allowlist so the wheel
# keeps changing volume instead of being consumed by a window that cannot scroll.
for name in ('searchbox_dialog','tidal_searchbox_dialog'):
    m=Machine(); m.top=m.node('window',name,[m.node('view')])
    assert m.call()==0 and not m.moved(); passed()

# iPod: a confirm dialog's buttons are its rows (contexts.inc BUTTONS). The wheel moves between the
# side-by-side buttons without scrolling anything, the bar is the button's own tile, the ends are
# hard and Centre clicks the selected button. The Stock build leaves the dialog stock: the wheel is volume.
m=Machine(); d=m.node('dialog','confirminfo_dialog'); m.word(d+O['W_PARENT'],m.wm)
m.word(d+O['W_W'],375); m.word(d+O['W_H'],320); m.clip=(0,0,375,320); m.top=d
buttons=[m.entry(d,220) for _ in range(2)]; m.nodes[d]['children']=buttons
pair=(53,242) if variant=='ipod' else (56,240)  # iPod's confirminfo_dialog.bin centres each in its half
for b,x in zip(buttons,pair): m.word(b+O['W_X'],x); m.word(b+O['W_W'],80); m.word(b+O['W_H'],80)
if variant=='ipod':
    m.paint(d); assert m.selected(d)==0 and m.sel()==(53,220,80,80)
    # The focused tile is framed in constant white (no accent lookup), over the bar; stroke color restored.
    assert [s[:4] for s in m.strokes]==[(53,220,80,80),(54,221,78,78)] and {s[5] for s in m.strokes}=={0xffffffff}
    assert m.lcd_colors()==LCD_COLORS
    assert m.call()==11 and m.selected(d)==1 and not m.moved()
    m.paint(d); assert m.sel()==(242,220,80,80) and m.clip==(0,0,375,320)
    assert [s[:4] for s in m.strokes]==[(242,220,80,80),(243,221,78,78)]
    assert m.call()==11 and m.selected(d)==1 and not m.moved()
    assert m.call(O['KEY_PREV'])==11 and m.selected(d)==0
    assert m.confirm()==11 and m.dispatched()[0][1]==buttons[0]; passed()
    # A wide button (autoshutdown's Cancel) gets the full-width bar.
    m.nodes[d]['name']='autoshutdown_dialog'; m.nodes[d]['children']=[buttons[0]]; m.word(buttons[0]+O['W_W'],287)
    m.paint(d); assert m.sel()==(0,220,375,80) and not m.strokes; passed()
else:
    assert m.call()==0 and m.call(O['KEY_CENTER'])==0 and not m.moved(); passed()

# An interrupted recall glide keeps the remembered row instead of adopting a visible one.
m,w,es=walk(10,5); w2,es2=m.page_list(10,extent=1000); m.glide=False
assert m.touch()==0
m.paint(w2); assert m.selected(w2)==5
w3,es3=m.page_list(10,extent=1000)
assert m.paint(w3)==0 and m.selected(w3)==5; passed()
# A wheel detent after recreation interrupts recall and sets the offset immediately.
m,w,es=walk(10,5); w2,es2=m.page_list(10,extent=1000); m.glide=False
assert m.call()==11 and m.selected(w2)==6
assert m.moved()[-1][0]=='scroll_view_set_offset' and m.moved()[-1][3]==252; passed()

# A live list whose row count changes resets the selection without re-reading the table.
m,w,es=walk(10,5)
m.nodes[w]['children']=es[:6]; m.word(w+O['VIEW_CONTENT_H'],6*48); m.word(w+O['SCROLL_Y'],0)
m.paint(w); assert m.selected(w)==0; passed()

# Virtual music tables recall a logical row and scroll to it on recreation.
m=Machine(); w,_,entries=m.table_page()
m.paint(w)
for _ in range(2): assert m.call()==11
assert m.selected(w)==2 and m.get(w+O['TABLE_TOP'])==60
m.rebind=None
w2,_,entries2=m.table_page()
m.paint(w2); assert m.selected(w2)==2 and m.get(w2+O['TABLE_TOP'])==60
assert m.confirm()==11 and m.dispatched()[0][1]==entries2[2]; passed()

# Table recall survives interruption even when the target is outside the recycled row pool.
for wanted in (2,12):
    m=Machine(); w,rs,es=m.table_page(n=20); m.paint(w)
    for _ in range(wanted): m.call()
    w2,rs2,es2=m.table_page(); m.glide=False
    m.touch(); m.paint(w2)
    assert m.selected(w2)==wanted and m.moved()[-1][2]==(wanted-1)*48+12
    # Rebind the pool as the restarted glide completes.
    top=(wanted-1)*48+12
    m.word(w2+O['TABLE_TOP'],top); m.word(w2+O['TABLE_ANIMATOR'],0)
    m.bind(rs2,top)
    m.paint(w2); assert m.selected(w2)==wanted
    m.confirm(); assert m.dispatched()[0][1]==es2[1]
    passed()
# A wheel during recall advances the logical target; an explicit tap instead replaces it.
m=Machine(); w,rs,es=m.table_page(n=20); m.paint(w)
for _ in range(12): m.call()
w2,rs2,es2=m.table_page(rebind=True); m.glide=False
m.touch(); m.call(); assert m.selected(w2)==13
m.click(es2[0]); assert m.get(w2+O['TABLE_ANIMATOR'])==0
m.paint(w2)
assert m.selected(w2)==12
m.confirm(); assert m.dispatched()[0][1]==es2[0]; passed()

# A synchronous restore rebind must discard the pre-scroll pool before centre dispatch.
m=Machine(); w,rs,es=m.table_page(n=20); m.paint(w)
for _ in range(12): m.call()
w2,rs2,es2=m.table_page(rebind=True)
m.paint(w2); assert m.selected(w2)==12
m.confirm(); assert m.dispatched()[0][1]==es2[1]; passed()

# Home keeps its native carousel presentation and value (including touch changes).
m=Machine(); w=m.page('home_page','slide_menu'); m.word(w+O['SLIDE_INDEX'],1)
child=[m.entry(w),m.entry(w)]; m.nodes[w]['children']=child
assert m.paint(w)==0 and not m.drawn()
assert any(c[0]=='stock_paint' for c in m.calls)
assert m.confirm()==11 and m.dispatched()[0][1]==child[1]
m.word(w+O['SLIDE_INDEX'],0)
assert m.confirm()==11 and m.dispatched()[0][1]==child[0]; passed()
# Long-press/boot release must reach stock cleanup, never activate a menu item.
for addr in [syms['g_power_longkey'],syms['g_ingore_bootkey_flag'],O['BOOT_KEY_GUARD']]:
    m.byte(addr,1); assert m.confirm()==0 and not m.dispatched(); m.byte(addr,0); passed()
# A single release confirms exactly once at DOUBLE_CLICK_MS, never a millisecond before.
m=Machine(); w,es=m.page_list(3)
assert m.release()==11 and not m.clicks
m.advance(DC-1); assert not m.clicks
m.advance(1); assert m.clicks==[es[0]] and not m.timers and not m.screens
m.advance(1000); assert m.clicks==[es[0]]; passed()
# Stock's wheel lockout after the centre key ends when the press opens its row: the next tick moves.
m=Machine(); w,es=m.page_list(3)
assert m.release()==11 and m.u.mem_read(O['KEY_LOCKOUT'],1)==b'\x08'
m.advance(DC); assert m.clicks==[es[0]] and m.u.mem_read(O['KEY_LOCKOUT'],1)==b'\0'
assert m.call(gap=0,debounce=True)==11 and m.selected(w)==1; passed()
# Before the deadline, the second release cancels the click and executes stock screen-off.
for gap in (0,100,DC-1):
    m=Machine(); w,es=m.page_list(3)
    assert m.release()==11 and m.release(gap)==0
    assert m.screens==[0] and not m.u.mem_read(syms['g_backlight_status'],1)[0]
    m.advance(1000); assert not m.clicks and not m.timers
    # Waking defaults to a double click, with no menu activation.
    assert m.release()==11 and m.screens==[0]
    assert m.release(100)==11 and m.screens==[0,1]
    assert m.u.mem_read(syms['g_backlight_status'],1)[0]==1 and not m.clicks; passed()
# At/after expiry, the first single has dispatched; the next release starts a new single.
for gap in (DC,DC+1):
    m=Machine(); w,es=m.page_list(3)
    assert m.release()==11 and m.release(gap)==11 and m.clicks==[es[0]]
    m.advance(300); assert m.clicks==[es[0],es[0]] and not m.screens; passed()
# Touch and either wheel direction cancel pending confirmation, including invalid navigation.
for action in ('touch','next','prev'):
    m=Machine(); w,es=m.page_list(3); m.release()
    if action=='touch': m.call(address=HOOKS['on_wm_tsdown_before_fun'][0],gap=100)
    else: m.call(O['KEY_NEXT'] if action=='next' else O['KEY_PREV'],gap=100)
    m.advance(1000); assert not m.clicks and not m.timers
    assert m.release()==11
    m.advance(300); assert len(m.clicks)==1 and not m.screens; passed()
# Changed, destroyed, or address-reused targets cannot receive a delayed click.
for change in ('top','surface','scope','selection','count','text','hidden','disabled',
               'animating','pressed','screen_off','locked','long','boot','reused_top','reused_surface'):
    m=Machine(); w,es=m.page_list(3,name='folder_page')
    m.folder('/first')
    m.nodes[es[0]]['text']='First'
    m.release()
    if change=='top': m.page_list(3,name='display_page')
    elif change=='surface': m.nodes[m.top]['children']=[m.node()]
    elif change=='scope': m.folder('/other')
    elif change=='selection': m.nodes[w]['_ringnav_index']=1
    elif change=='count': m.nodes[w]['children']=es[:2]
    elif change=='text': m.nodes[es[0]]['text']='Rebound'
    elif change=='hidden': m.nodes[w]['visible']=0
    elif change=='disabled': m.nodes[es[0]]['enable']=0
    elif change=='animating': m.animating=1
    elif change=='pressed': m.pressed=1
    elif change=='screen_off': m.byte(syms['g_backlight_status'],0)
    elif change=='locked': m.byte(syms['g_lockscreen_pageflag'],1)
    elif change=='long': m.byte(syms['g_power_longkey'],1)
    elif change=='boot': m.byte(O['BOOT_KEY_GUARD'],1)
    else: m.nodes[m.top if change=='reused_top' else w].pop('_ringnav_confirm')
    m.advance(300); assert not m.clicks and not m.timers,change; passed()
# Non-virtual rows cannot inherit confirmation merely by sharing an index and text.
for kind in ('scroll_view','slide_menu'):
    for text in ('','Same title'):
        for reused in (False,True):
            m=Machine(); w,es=m.page_list(3)
            m.nodes[w]['type']=kind
            if kind=='slide_menu': m.word(w+O['SLIDE_INDEX'],0)
            m.nodes[es[0]]['text']=text
            m.release()
            if reused:
                m.nodes[es[0]].pop('_ringnav_confirm',None)
            else:
                replacement=m.entry(w)
                m.nodes[replacement]['text']=text
                m.nodes[w]['children'][0]=replacement
            m.advance(300)
            assert not m.clicks and not m.timers,(kind,text,reused)
            passed()

# A recycled table pool resolves the original logical index from the live row mapping.
m=Machine(); w,rs,es=m.table_page(); m.release()
m.word(rs[0]+O['ROW_INDEX'],1); m.word(rs[1]+O['ROW_INDEX'],0)
m.advance(300); assert m.clicks==[es[1]]; passed()
m=Machine(); w,rs,es=m.table_page(); m.release(); m.bind(rs,480)
m.advance(300); assert not m.clicks; passed()
# Invalid state observed before expiry stays cancelled even if the same page returns.
m=Machine(); w,es=m.page_list(3); m.release(); m.animating=1
m.call(gap=100); m.animating=0; m.advance(300)
assert not m.clicks and not m.timers; passed()
# Timer allocation failure consumes both releases without activation or screen toggling.
m=Machine(); w,es=m.page_list(3); m.timer_fail=True
assert m.release()==11 and m.release(100)==11
m.advance(1000); assert not m.clicks and not m.screens and not m.timers; passed()
# Empty menus never arm confirmation.
m=Machine(); m.page()
assert m.release()==11 and m.release(100)==11
assert not m.timers and not m.clicks and not m.screens; passed()
# Audit the stock creation path independently: live offset, duration, easing,
# completion address and deselection agree with the fields used by the patch.
m=Machine(); w=m.page('home_page','slide_menu')
m.call(address=0x5f3400,args=(w,-240,0,0),gap=0)
a=m.get(w+O['SLIDE_ANIMATOR'])
assert m.get(a+O['ANIM_DURATION'])==150 and signed(m.get(a+O['ANIM_X_TO']))==-240
assert m.slide_callbacks[a]==(syms['slide_menu_on_scroll_done'],w)
assert m.nodes[m.nodes[w]['children'][0]]['focused']==0
m.advance(150); assert m.get(w+O['SLIDE_INDEX'])==1 and not m.slides; passed()

# Execute native parameter writes and stock completion; only the scheduler is
# deterministic. Check live origin, destination, duration, focus and one active animator.
def slide(m,w):
    a=m.get(w+O['SLIDE_ANIMATOR'])
    assert a and list(m.slides)==[a]
    return a,signed(m.get(a+0x68)),signed(m.get(a+O['ANIM_X_TO'])),m.get(a+O['ANIM_DURATION'])

for start in (0,0xfffffff0):
    for key,direction in ((O['KEY_NEXT'],1),(O['KEY_PREV'],-1)):
        for count in (3,4,5):
            m=Machine(); m.now=start; w=m.page('home_page','slide_menu')
            assert m.call(key,gap=0)==11
            a,origin,goal,duration=slide(m,w)
            assert (origin,goal,duration)==(0,-direction*240,200)
            for i in range(1,count):
                m.advance(30); live=signed(m.get(w+O['SLIDE_OFFSET']))
                assert m.call(key,gap=0)==11
                assert slide(m,w)==(a,live,-direction*240*(i+1),120)
            m.advance(120)
            assert not m.slides and m.get(w+O['SLIDE_ANIMATOR'])==0
            assert m.get(w+O['SLIDE_OFFSET'])==0
            assert m.get(w+O['SLIDE_INDEX'])==(direction*count)%7
            assert m.nodes[m.nodes[w]['children'][(direction*count)%7]]['focused']==1
            m.advance(126); m.call(key,gap=0)
            assert slide(m,w)[3]==200
            m.advance(200); settled=m.get(w+O['SLIDE_INDEX']); m.advance(500)
            assert m.get(w+O['SLIDE_INDEX'])==settled and not m.slides
            passed()
# Reversal at either speed starts at the live position and actually travels backward,
# even when multiple intended destinations have accumulated.
for fast in (False,True):
    for key,reverse,sign in ((O['KEY_NEXT'],O['KEY_PREV'],1),(O['KEY_PREV'],O['KEY_NEXT'],-1)):
        m=Machine(); w=m.page('home_page','slide_menu'); m.call(key,gap=0)
        if fast:
            for _ in range(3): m.call(key,gap=20)
        m.advance(30); live=signed(m.get(w+O['SLIDE_OFFSET']))
        a=m.get(w+O['SLIDE_ANIMATOR']); m.call(reverse,gap=0)
        active,origin,goal,duration=slide(m,w)
        assert active==a and origin==live and duration==200 and (goal-live)*sign>0
        m.advance(200)
        assert m.get(w+O['SLIDE_INDEX'])==(-goal//240)%7 and not m.slides
        assert m.nodes[m.nodes[w]['children'][(-goal//240)%7]]['focused']==1
        passed()
# Exact fast-window boundary, stock rejection, and interaction resets.
for gap,want in ((200,120),(201,200)):
    m=Machine(); w=m.page('home_page','slide_menu'); m.call(gap=0); m.call(gap=gap)
    assert slide(m,w)[3]==want; passed()
for action in ('touch','center','click','leave','animating','pressed','unusable','missing'):
    m=Machine(); w=m.page('home_page','slide_menu'); home=m.top; m.call(gap=0)
    if action=='touch': m.call(address=HOOKS['on_wm_tsdown_before_fun'][0],gap=20)
    elif action=='center': m.release(20)
    elif action=='click': m.click(m.nodes[w]['children'][0],gap=20)
    elif action=='leave': m.page('playing_page'); m.call(gap=20); m.top=home
    elif action=='unusable':
        m.byte(syms['g_backlight_status'],0); m.call(gap=20); m.byte(syms['g_backlight_status'],1)
    elif action=='missing': m.call(args=(m.wm,0,0,0),gap=20)
    else: setattr(m,action,1); m.call(gap=20); setattr(m,action,0)
    assert m.call(gap=1)==11 and slide(m,w)[3]==200; passed()
m=Machine(); w=m.page('home_page','slide_menu'); m.call(gap=0)
m.byte(0xa37c89,1); assert m.call(O['KEY_PREV'],gap=20,debounce=True)==11
assert not m.moved(); m.call(gap=20); assert slide(m,w)[3]==120; passed()
# Animator and callback allocation failures settle through stock selection.
for failure in ('slide_fail','slide_on_fail'):
    m=Machine(); w=m.page('home_page','slide_menu'); setattr(m,failure,True)
    m.call(gap=0)
    assert m.get(w+O['SLIDE_INDEX'])==1 and not m.get(w+O['SLIDE_ANIMATOR'])
    assert not m.slides and not m.slide_callbacks
    assert m.nodes[m.nodes[w]['children'][1]]['focused']==1
    setattr(m,failure,False); m.call(gap=20); assert slide(m,w)[3]==200; passed()
# Centre arms the intended final icon while its slide is still unfinished.
m=Machine(); w=m.page('home_page','slide_menu')
for _ in range(5): m.call(gap=20)
m.release(0); m.advance(75); m.advance(125)
assert m.clicks==[m.nodes[w]['children'][5]] and not m.slides, (m.clicks,m.get(w+O['SLIDE_INDEX']),m.timers,m.slides); passed()
m=Machine(); w=m.page('home_page','slide_menu'); m.release(0); m.call(gap=20)
m.advance(300); assert not m.clicks; passed()
# Other slide menus retain stock next/previous handling.
m=Machine(); m.page('sysset_page','slide_menu')
assert m.call(gap=0)==11 and m.call(gap=1)==11 and m.moved(); passed()
# Even if the UI services a release before an overdue timer, both singles confirm once.
m=Machine(); w,es=m.page_list(3); m.release(); m.now+=201
assert m.release()==11 and m.clicks==[es[0]] and len(m.timers)==1
m.advance(300); assert m.clicks==[es[0],es[0]] and not m.screens; passed()
# Deadline expiry may navigate: the following release must resolve the new menu.
m=Machine(); w,es=m.page_list(3)
def navigate(a,b):
    m.on_click=None
    m.page_list(3,name='display_page')
m.on_click=navigate; m.release(); old=m.top
assert m.release(201)==11 and m.top!=old and len(m.clicks)==1
m.advance(300); assert len(m.clicks)==2 and m.clicks[0]!=m.clicks[1] and not m.screens; passed()
# The downstream stock cleanup still owns long-press and boot-key releases.
for addr in (syms['g_power_longkey'],syms['g_ingore_bootkey_flag'],O['BOOT_KEY_GUARD']):
    m=Machine(); m.page_list(3); m.byte(addr,1)
    assert m.release()==0
    m.advance(300)
    assert not m.clicks and not m.screens and not m.timers and m.u.mem_read(addr,1)==b'\0'; passed()
# Play/Pause reaches the real stock downstream handler, including on a supported page.
m=Machine(); m.page_list(3)
assert m.call(O['KEY_PLAY'])==0
m.call(O['KEY_PLAY'],address=syms['on_wm_keyup_fun'],gap=0)
assert any(c[0]=='playpause_quick_click' for c in m.calls) and not m.clicks; passed()
# A root-window paint observes leaving navigation even when the new page has no pane.
m=Machine(); w,es=m.page_list(3); m.release(); old=m.top
m.top=m.node('window','playing_page'); m.paint(m.top,gap=100)
m.top=old; m.advance(300); assert not m.clicks and not m.timers; passed()
m=Machine(); w=m.page('home_page','slide_menu'); home=m.top; m.call(gap=0)
m.top=m.node('window','playing_page'); m.paint(m.top,gap=20)
m.top=home; assert m.call(gap=1)==11 and m.moved(); passed()
# Local query and home selection changes invalidate pending confirmation too.
m=Machine(); w,rs,es=m.table_page(); m.release()
m.word(syms['g_class_type'],0xf002); m.advance(300); assert not m.clicks; passed()
m=Machine(); w=m.page('home_page','slide_menu')
m.word(w+O['SLIDE_INDEX'],0); m.nodes[w]['children']=[m.entry(w),m.entry(w)]
m.release(); m.word(w+O['SLIDE_INDEX'],1); m.advance(300); assert not m.clicks; passed()
# Unsigned milliseconds may wrap while confirmation is pending.
m=Machine(); m.now=0xfffffff0; m.page_list(3)
assert m.release()==11 and m.release(DC-1)==0
m.advance(300); assert not m.clicks and m.screens==[0]; passed()
# Short lists stay precise; virtual tables use total logical rows, not their row pool.
for table in (False,True):
    for count in (0,1,16,17):
        m=Machine()
        if table:
            # A dynamic page keeps hard ends, so this stays an acceleration check.
            w,rs,es=m.table_page(n=min(count,4),name='playerqueue_page',rebind=True)
            m.word(w+O['TABLE_ROWS'],count)
        else: w,es=m.page_list(count,extent=count*48)
        m.paint(w)
        if count==17:
            # Seventeen rows accelerate even at a 50ms cadence: the second tick is still one
            # row (50ms of spin), the third crosses 100ms and steps two. iPod's row lists wait
            # for 300ms of spin: six one-row ticks, then the seventh steps two.
            for want in (1,2,4) if variant=='stock' else (1,2,3,4,5,6,8):
                assert m.call(gap=50)==11 and m.selected(w)==want
            for _ in range(20): m.call(gap=50)
            assert m.selected(w)==count-1      # held at the end
            assert m.call(O['KEY_PREV'],gap=50)==11 and m.selected(w)==count-2 # ramp resets there
        else:
            for want in (1,2,3):
                assert m.call(gap=50)==11 and m.selected(w)==min(want,count-1)
        if count==16:
            for want in range(4,16):
                assert m.call(gap=50)==11 and m.selected(w)==want
            for want in range(14,-1,-1):
                assert m.call(O['KEY_PREV'],gap=50)==11 and m.selected(w)==want
            assert m.call(gap=50)==11 and m.selected(w)==1 # and at the start
        passed()

# Resizing across the short-list boundary cannot carry a previous fast run with it.
m=Machine(); w,es=m.page_list(17,height=960,extent=17*48)
m.nodes[w]['children']=es; m.paint(w)
for want in (1,3,6) if variant=='stock' else (1,1,2,4,6):  # iPod: the overshoot tick, then two rows from 300ms of spin
    assert m.call(gap=100)==11 and m.selected(w)==want
m.nodes[w]['children']=es[:16]; m.paint(w)
assert m.call(gap=100)==11 and m.selected(w)==1   # fresh one-row run on the short list
for want in (2,3) if variant=='stock' else (1,2):
    assert m.call(gap=100)==11 and m.selected(w)==want
m.nodes[w]['children']=es; m.paint(w)
assert m.call(gap=100)==11 and m.selected(w)==1   # and again after growing back
for want in (3,) if variant=='stock' else (1,2,4):
    assert m.call(gap=100)==11 and m.selected(w)==want
passed()

# Sustained ticks ramp one row per 100ms of same-direction spin up to eight rows, then hold;
# pauses, spacing past the 140ms window and reversal reset. Byte-exact boundaries, wrapped clock.
# iPod's row lists ramp gently (navigation.c LIST_FIRST_MS, LIST_RAMP_MS): one row until 300ms of
# spin, two from there and one more per further 200ms, eight from 1500ms. IPOD_SPIN is (gap since
# the last tick, rows per step then); every gap is inside the 140ms window, so the run is one.
IPOD_SPIN=((0,1),(100,1),(100,1),(99,1),(1,2),(100,2),(99,2),(1,3),(100,3),(99,3),(1,4),(100,4),(99,4),
           (1,5),(100,5),(99,5),(1,6),(100,6),(99,6),(1,7),(100,7),(99,7),(1,8),(100,8))
for table in (False,True):
    for start in (0,0xfffffff0):
        m=Machine(); m.now=start
        w,rs=m.list_surface(table,n=500)
        if table: m.word(w+O['TABLE_ROWS'],500)
        # 99ms of spin is still one row; the next millisecond enters two-row speed, and each
        # further 100ms adds one row until the eight-row ceiling holds.
        seq=((0,1),(99,2),(1,4),(99,6),(1,9),(99,12),(1,16),(99,20),(1,25),(99,30),(1,36),
             (99,42),(1,49),(99,56),(1,64),(99,72),(1,80))
        if variant=='ipod':  # 299/300, 499/500 ... 1499/1500ms: 1,1,2,3,5,7,9,12 ... 92,100; the second tick
            # is the overshoot filter's (it times the run all the same)
            seq=[(gap,sum(g for j,(_,g) in enumerate(IPOD_SPIN[:i+1]) if j!=1)) for i,(gap,_) in enumerate(IPOD_SPIN)]
        for gap,want in seq:
            assert m.call(gap=gap)==11 and m.selected(w)==want
        # 140ms still counts as spinning, 141ms breaks the run and drops back to one row.
        assert m.call(gap=140)==11 and m.selected(w)==want+8
        assert m.call(gap=141)==11 and m.selected(w)==want+9
        assert m.call(O['KEY_PREV'],gap=1)==11 and m.selected(w)==want+8
        passed()
# Pixel-scroll fallback keeps the 100ms ramp in both variants, and immediate offsets.
m=Machine(); w=m.page(t='table_client')
for want in (48,144,288,480,720,1008,1344,1728):
    assert m.call(gap=100)==11 and m.get(w+O['TABLE_TOP'])==want
assert m.call(gap=141)==11 and m.get(w+O['TABLE_TOP'])==1776; passed()
def wiped(m,w):  # a destroyed surface recreated at the same address has no widget-owned props
    for k in [k for k in m.nodes[w] if k.startswith('_ringnav')]: del m.nodes[w][k]
def disturb(m,w,change,n):
    """One interruption of the n-row list w between wheel ticks; returns the live surface."""
    if change=='window': w,_=m.page_list(n,extent=n*48,name='allmusic_page')
    elif change=='pane':
        old=m.top; w,_=m.page_list(n,extent=n*48,name='allmusic_page'); m.nodes[old]['children']=[w]; m.top=old
    elif change=='scope': m.word(syms['g_class_type'],0xf002)
    elif change=='context': m.nodes[m.top]['name']='display_page'
    elif change=='count': m.nodes[w]['children'].pop()
    elif change=='recreated': wiped(m,w)
    elif change in ('gesture','animating'):
        field='pressed' if change=='gesture' else 'animating'
        setattr(m,field,1); assert m.call(gap=10)==11; setattr(m,field,0)
    elif change=='screen':
        m.byte(syms['g_backlight_status'],0); assert m.call(gap=10)==0; m.byte(syms['g_backlight_status'],1)
    elif change=='unsupported':  # the same tick is a volume step on Now Playing
        m.nodes[m.top]['name']='playing_page'; assert m.call(gap=10)==0; m.nodes[m.top]['name']='allmusic_page'
    elif change=='rejected': m.byte(0xa37c89,1); assert m.call(gap=10,debounce=True)==11
    elif change=='touch': m.call(address=HOOKS['on_wm_tsdown_before_fun'][0],event_type=O['EVT_POINTER_DOWN'],gap=10)
    elif change=='click': m.click(m.node('button'),gap=10)
    elif change=='missing': m.call(args=(0,0,0,0),gap=10)
    elif change=='centre': m.call(O['KEY_CENTER'],gap=10)
    elif change=='return': assert m.call(O['KEY_RETURN'],gap=10)==0
    elif change=='play': assert m.call(O['KEY_PLAY'],gap=10)==0
    elif change=='long': long_return(m)
    elif change=='reverse': assert m.call(O['KEY_PREV'],gap=10)==11 and m.selected(w)==1
    return w
# A fast spin belongs to its live menu, pane and browsing scope.
for change in ('window','pane','scope','context','count','gesture','screen','unsupported','touch','click','missing','centre'):
    m=Machine(); w,es=m.page_list(40,extent=40*48,
        name='sysset_page' if change=='context' else 'allmusic_page')
    for _ in range(7): m.call(gap=100)
    assert m.selected(w)==(28 if variant=='stock' else 12)  # iPod: 1,0,1,2,2,3,3 rows
    w=disturb(m,w,change,40)
    m.paint(w,gap=0)
    before=m.selected(w)
    assert m.call(gap=50)==11 and m.selected(w)==before+1,change
    passed()
# Rejected stock wheel input resets a sustained list run.
m=Machine(); w,es=m.page_list(40,extent=40*48)
for _ in range(7): m.call(gap=100)
fast=28 if variant=='stock' else 12
assert m.selected(w)==fast
m.byte(0xa37c89,1)
assert m.call(gap=10,debounce=True)==11 and m.selected(w)==fast
assert m.call(gap=40)==11 and m.selected(w)==fast+1; passed()
# A click on a clickable child selects its collected ancestor, not a stale row.
m=Machine(); w=m.page(); m.word(w+O['W_H'],96)
row1=m.entry(w,0); row2=m.entry(w,96); deep=m.entry(row1,0)
m.nodes[row1]['children']=[deep]; m.nodes[w]['children']=[row1,row2]
m.paint(w); assert m.selected(w)==0
assert m.call(O['KEY_NEXT'])==11 and m.selected(w)==1
m.click(deep); assert m.selected(w)==0
m.click(m.node('button')); assert m.selected(w)==0; passed()
# Real canvas ABI, translation and clip code execute; only the LCD rectangle sink is mocked.
# A 20px row at canvas origin (7,20) under a (10,30)-(229,199) clip.
m=Machine(); w=m.page(); m.word(w+O['W_H'],96)
e=m.entry(w,0); m.word(e+O['W_H'],20); m.nodes[w]['children']=[e]
sink='lcd_stroke_rect' if variant=='stock' else 'lcd_fill_rect'
for name in ('canvas_get_clip_rect','canvas_set_clip_rect',
             *(('canvas_set_stroke_color','canvas_stroke_rect') if variant=='stock' else ('canvas_fill_rect',))):
    del m.handlers[syms[name]]
m.mock(sink)
m.word(m.lcd+0x3c,1); m.word(m.lcd+0xb0,240); m.word(m.lcd+0xb4,240)
m.word(m.canvas+O['CANVAS_X'],7); m.word(m.canvas+O['CANVAS_Y'],20)
for off,val in [(0x10,10),(0x14,30),(0x18,229),(0x1c,199)]: m.word(m.canvas+off,val)
m.paint(w)
if variant=='stock':
    # The square fallback (radius 9 needs more height) runs the stock square code.
    assert not m.rounded and [s[:4] for s in m.strokes]==[(8,21,238,18),(9,22,236,16)]
    assert m.strokes[0][4:]==((10,30,220,86),SHADE)
    assert m.strokes[1][4:]==((10,30,220,86),OUTLINE)
else:
    # The bands reach the LCD only for the row pixels inside the clip.
    assert [(c[2],c[3]) for c in m.calls if c[0]==sink]==[(10,y) for y in range(30,40)]
assert [m.get(m.canvas+off) for off in (0x10,0x14,0x18,0x1c)]==[10,30,229,199]
assert m.lcd_colors()==LCD_COLORS; passed()
# Wheel selection is immediate even when restoration animations would still be running.
m=Machine(); w,es=m.page_list(6)
m.paint(w); m.glide=False
m.call(); m.call(); assert m.selected(w)==2 and m.get(w+O['SCROLL_Y'])==60
m.paint(w); assert m.selected(w)==2
assert m.confirm()==11 and m.dispatched()[0][1]==es[2]
m.call(); assert m.selected(w)==3
m.call(O['KEY_PREV']); assert m.selected(w)==2
m.call(O['KEY_PREV']); assert m.selected(w)==1 and m.moved()[-1][3]==36
m.call(O['KEY_PREV']); assert m.selected(w)==0 and m.get(w+O['VIEW_ANIMATOR'])==0; passed()
# Empty menus never activate or turn off the screen; touch doesn't swallow its first event.
m=Machine(); w=m.page(); assert m.confirm()==11 and not m.dispatched()
assert m.touch()==0 and not m.dispatched(); passed()
# Clip an oversized target to its surface without losing its selection.
m=Machine(); w=m.page(); m.word(w+O['W_H'],96)
e=m.entry(w); m.word(e+O['W_H'],140); m.nodes[w]['children']=[e]
m.paint(w)
assert m.selected(w)==0 and m.sel()==(0,0,240,140)
assert {r['clip'] for r in m.rounded}|{b[5] for b in m.bands}=={(0,0,240,96)}; passed()
# Both list kinds leave breathing room, shrink it in tight viewports, and clamp at either end.
for virtual in (False,True):
    for height,margin in ((96,12),(60,6),(49,0),(48,0),(40,0)):
        m=Machine()
        if virtual:
            w,rs,es=m.table_page(n=10,rebind=True); m.word(w+O['TABLE_ROWS'],10); off=O['TABLE_TOP']
        else:
            w,es=m.page_list(10,extent=480); off=O['SCROLL_Y']
        m.word(w+O['W_H'],height); m.paint(w)
        assert m.get(w+off)==0 and not m.moved()
        for _ in range(3): m.call()
        assert m.selected(w)==3
        assert m.get(w+off)==(144 if height<48 else 192-height+margin)
        m.call(O['KEY_PREV'])
        assert m.selected(w)==2 and m.get(w+off)==96-margin
        m.call(O['KEY_PREV']); m.call(O['KEY_PREV'])
        assert m.selected(w)==0 and m.get(w+off)==0
        for _ in range(9): m.call()
        assert m.selected(w)==9 and m.get(w+off)==(432 if height<48 else 480-height)
        m.paint(w); assert not m.moved(); passed()
# Entering a tall row from either direction reveals its title, including after recreation.
m=Machine(); w,es=m.page_list(3,height=96,extent=400)
m.word(es[1]+O['W_H'],140); m.word(es[2]+O['W_Y'],188)
m.call(); assert m.selected(w)==1 and m.get(w+O['SCROLL_Y'])==48
m.call(); m.call(O['KEY_PREV'])
assert m.selected(w)==1 and m.get(w+O['SCROLL_Y'])==48
w,es=m.page_list(3,height=96,extent=400)
m.word(es[1]+O['W_H'],140); m.word(es[2]+O['W_Y'],188)
m.paint(w); assert m.selected(w)==1 and m.get(w+O['SCROLL_Y'])==48; passed()

# Small rows keep the square shade-plus-white outline; the rounded path starts only when both
# outer dimensions exceed 2*RADIUS. Tiny rows are skipped outright, never with negative sizes.
for ww,hh in [(240,20),(20,48),(6,6),(21,21)] if variant=='stock' else ():
    m=Machine(); w=m.page(); m.word(w+O['W_H'],96)
    e=m.entry(w,0); m.word(e+O['W_W'],ww); m.word(e+O['W_H'],hh); m.nodes[w]['children']=[e]
    assert m.paint(w)==0
    if ww>2*O['RADIUS']+2 and hh>2*O['RADIUS']+2:
        assert [r['kind'] for r in m.rounded]==['fill','stroke','stroke'] and not m.strokes,(ww,hh,m.rounded,m.strokes)
    else:
        assert not m.rounded and [s[:4] for s in m.strokes]==[(1,1,ww-2,hh-2),(2,2,ww-4,hh-4)]
        assert [s[5] for s in m.strokes]==[SHADE,OUTLINE]
    assert m.global_alpha==0 and m.clip==(0,0,240,240)
    assert m.get(m.lcd+O['LCD_STROKE_COLOR'])==0x12345678; passed()
m=Machine(); w=m.page(); m.word(w+O['W_H'],96)
e=m.entry(w,0); m.word(e+O['W_W'],4); m.word(e+O['W_H'],4); m.nodes[w]['children']=[e]
assert m.paint(w)==0 and not m.drawn() and m.clip==(0,0,240,240); passed()
# A page with no entries paints nothing and leaves every saved property alone.
m=Machine(); w=m.page()
assert m.paint(w)==0 and m.selected(w)==-1
assert not m.drawn() and m.global_alpha==0 and m.clip==(0,0,240,240)
assert m.lcd_colors()==LCD_COLORS; passed()

# Every saved value is unusual: the rounded path restores fill, stroke and clip exactly and
# never touches the canvas or LCD global-alpha bytes.
m=Machine(); w,es=m.page_list(3,extent=1000)
m.word(m.lcd+O['LCD_FILL_COLOR'],0x00000001); m.word(m.lcd+O['LCD_STROKE_COLOR'],0x80ffffff)
m.byte(m.canvas+0x0e,0x7f); m.byte(m.lcd+0xe4,0x11)
m.paint(w)
assert m.get(m.lcd+O['LCD_FILL_COLOR'])==0x00000001 and m.get(m.lcd+O['LCD_STROKE_COLOR'])==0x80ffffff
assert m.u.mem_read(m.canvas+0x0e,1)[0]==0x7f and m.u.mem_read(m.lcd+0xe4,1)[0]==0x11
assert m.global_alpha==0 and m.clip==(0,0,240,240); passed()

# Taps choose their pane even with no initial owner or with both panes previously selected.
for seeded in ((),(0,),(0,1)):
    m=Machine(); a,b,ae,be=m.panes()
    nested=m.entry(be[1]); m.nodes[be[1]]['children']=[nested]
    for i in seeded: m.nodes[(a,b)[i]]['_ringnav_index']=0
    m.touch(); m.click(nested)
    assert m.selected(a)==-1 and m.selected(b)==1
    m.paint(a); assert not m.drawn()
    m.paint(b); assert not m.drawn()
    m.confirm(); assert m.dispatched()[0][1]==be[1]
    m.call(); assert m.selected(b)==2
    m.touch(); m.click(ae[0]); m.confirm()
    assert m.selected(b)==-1 and m.dispatched()[0][1]==ae[0]
    passed()

# An inactive pane is never painted, so the outline cannot appear on two panes at once.
m=Machine(); a,b,_,_=m.panes(96); m.nodes[a]['_ringnav_index']=0
m.paint(b); assert not m.drawn()
m.paint(a); assert m.sel()==(0,0,240,48); passed()

# A canvas backend that declines the rounded stroke keeps the outline via the square fallback.
if variant=='stock':
    m=Machine(); w,es=m.page_list(3,extent=1000); m.rounded_fail=True
    m.paint(w)
    assert [r['kind'] for r in m.rounded]==['fill','stroke'] and [s[:4] for s in m.strokes]==[(1,1,238,46),(2,2,236,44)]
    assert m.rounded[1]['color']==SHADE
    assert [s[5] for s in m.strokes]==[SHADE,OUTLINE]
    assert m.lcd_colors()==LCD_COLORS
    assert m.global_alpha==0 and m.clip==(0,0,240,240); passed()

# The real stock rounded wrappers run with canvas services mocked. This firmware declines with
# RET_FAIL when the canvas has no vgcanvas, and changes no fill/stroke color or alpha byte.
m=Machine(); m.probe_canvas(rect=(1,1,238,46))
assert m.call(address=syms['canvas_fill_rounded_rect'],args=(m.canvas,m.rect,0,m.color),stack=(O['RADIUS'],))==2
assert m.call(address=syms['canvas_stroke_rounded_rect'],args=(m.canvas,m.rect,0,m.color),stack=(O['RADIUS'],1))==2
assert m.lcd_colors()==LCD_COLORS
assert m.global_alpha==0 and not m.vg_calls; passed()

# With a vgcanvas present the same wrappers take the AGGE-facing path the Q2 firmware links:
# the color pointer and the radius/border-width stack slots reach the backend, canvas colors and
# the canvas alpha byte stay put. (The fill call then enters stock FP64 dither math that Unicorn's
# MIPS32 FR=0 FPU cannot execute; its vgcanvas calls up to that point are still asserted.)
m=Machine(); m.probe_canvas(vg=0x1000400,color=0xffffffff)
assert m.call(address=syms['canvas_stroke_rounded_rect'],args=(m.canvas,m.rect,0,m.color),stack=(O['RADIUS'],2))==0
names=[c[0] for c in m.vg_calls]
assert 'vgcanvas_set_stroke_color' in names and 'vgcanvas_set_line_width' in names
assert [c[2]&0xffffffff for c in m.vg_calls if c[0]=='vgcanvas_set_stroke_color']==[0xffffffff]
assert [struct.unpack('<f',struct.pack('<I',c[2]&0xffffffff))[0] for c in m.vg_calls if c[0]=='vgcanvas_set_line_width']==[2.0]
assert m.lcd_colors()==LCD_COLORS
assert m.global_alpha==0 and m.u.mem_read(m.canvas+0x0e,1)[0]==0; passed()
m=Machine(); m.probe_canvas(vg=0x1000400)
try: m.call(address=syms['canvas_fill_rounded_rect'],args=(m.canvas,m.rect,0,m.color),stack=(O['RADIUS'],))
except UcError: pass
assert [c[2] for c in m.vg_calls if c[0]=='vgcanvas_set_fill_color']==[FILL]
assert m.get(m.lcd+O['LCD_FILL_COLOR'])==0x9abcdef0 and m.global_alpha==0
assert not m.allocs; passed()

# CPU-LCD branch: the stock fill has a real rasterizer that Unicorn can run end to end. With a
# non-color LCD type it fills the rounded rect through LCD sinks, frees every allocation, and
# declines radius <= 2 exactly as the stock code does.
m=Machine(); m.probe_canvas(lcd_type=0)
m.mock('lcd_fill_rect','lcd_draw_hline','lcd_draw_vline',prefix='sink:')
assert m.call(address=syms['canvas_fill_rounded_rect'],args=(m.canvas,m.rect,0,m.color),stack=(O['RADIUS'],))==0
ops=[c for c in m.calls if c[0].startswith('sink:')]
assert ops and all(10<=signed(c[2])<=109 and 10<=signed(c[3])<=49 for c in ops)
assert not m.allocs
assert m.call(address=syms['canvas_fill_rounded_rect'],args=(m.canvas,m.rect,0,m.color),stack=(2,))==2
assert not [c for c in m.calls if c[0].startswith('sink:')]; passed()
# The actual stock filter runs first, including each screen-off lock mode.
for backlight in [0,1]:
    for mode in range(4):
        for key in [170,O['KEY_PLAY'],O['KEY_PREV'],O['KEY_NEXT'],O['KEY_CENTER'],222,223,42]:
            results=[]
            for patched in [False,True]:
                m=Machine(patched); m.page('playing_page')
                m.byte(syms['g_backlight_status'],backlight)
                m.byte(syms['g_keylock_flag'],1); m.byte(syms['g_keylock_mode'],mode)
                results.append(m.call(key))
            assert results[0]==results[1],(backlight,mode,key,results)
            passed()
# Non-ring keys on supported pages must pass through unchanged.
for key in [0,13,170,O['KEY_PLAY'],222,223,0xffffffff]:
    m=Machine(); m.page(); assert m.call(key)==0 and not m.moved(); passed()
# A tap on a recreated table must not recall/rebind its target before native delivery.
for pointer_down in (False,True):
    for virtual in (False,True):
        m=Machine()
        if virtual: w,rs,es=m.table_page(n=20)
        else: w,es=m.page_list(20)
        m.paint(w)
        for _ in range(12): m.call()
        if virtual: w,rs,es=m.table_page(rebind=True)
        else: w,es=m.page_list(20)
        off=O['TABLE_TOP'] if virtual else O['SCROLL_Y']
        if pointer_down:
            m.touch()
            assert m.get(w+off)==0 and not m.moved(), 'pointer-down recalled selection'
            m.pressed=1; m.paint(w)
            assert m.get(w+off)==0 and not m.moved(), 'paint recalled under pointer'
            m.pressed=0
        def check_tapped_row(a,b):
            if virtual: assert m.get(rs[0]+O['ROW_INDEX'])==0, 'tap rebound'
        m.on_click=check_tapped_row
        m.click(es[0])
        assert m.selected(w)==0 and m.get(w+off)==0 and m.clicks==[es[0]]
        assert not m.moved(); passed()

# Saturate before adding: large valid tables cannot wrap their viewport or logical selection.
for pooled in (False,True):
    m=Machine(); w,rs,es=m.table_page(n=1 if pooled else 0,name='playerqueue_page')
    m.word(w+O['ROW_HEIGHT'],1); m.word(w+O['TABLE_ROWS'],0x7fffffff)
    m.word(w+O['W_H'],1); m.word(w+O['TABLE_TOP'],0x7ffffffe)
    if pooled:
        m.word(rs[0]+O['ROW_INDEX'],0x7ffffffe)
        m.word(rs[0]+O['W_Y'],0x7ffffffe); m.word(es[0]+O['W_H'],1)
    m.paint(w)
    for _ in range(9):
        assert m.call(gap=50)==11 and m.get(w+O['TABLE_TOP'])==0x7ffffffe
        if pooled: assert m.selected(w)==0x7ffffffe
    passed()

# A pages widget inside a navigation surface exposes only its active child's targets.
for active in (-1,0,1,2):
    m=Machine(); w=m.page()
    tabs=m.node('pages',active=active); m.word(tabs+O['W_PARENT'],w)
    es=[m.entry(tabs) for _ in range(2)]
    m.nodes[tabs]['children']=es; m.nodes[w]['children']=[tabs]
    m.paint(w); m.confirm()
    assert m.clicks==([es[active]] if active in (0,1) else []),active
    passed()

# If the white rounded stroke fails, draw the square fallback as well.
if variant=='stock':
    m=Machine(); w,es=m.page_list(3); m.rounded_fail=O['RADIUS']-1
    m.paint(w)
    assert [s[5] for s in m.strokes]==[SHADE,OUTLINE]
    assert m.lcd_colors()==LCD_COLORS; passed()

# An overdue confirmation can change power state before the next release is processed.
for flag in ('g_backlight_status','g_power_longkey','g_ingore_bootkey_flag'):
    m=Machine(); w,es=m.page_list(3)
    m.on_click=lambda a,b: m.byte(syms[flag],0 if flag=='g_backlight_status' else 1)
    m.release(); m.now+=201
    assert m.call(O['KEY_CENTER'],gap=0)==0,flag
    assert m.clicks==[es[0]] and not m.timers
    passed()

# Reject a missing key event before calling the stock filter, cancelling pending input.
m=Machine(); w,es=m.page_list(3); m.release()
assert m.call(args=(0,0,0,0),gap=0)==0
m.advance(300); assert not m.clicks and not m.timers; passed()

# Any native click supersedes delayed confirmation, even without a pointer-down callback.
for target_kind in ('same', 'nested', 'outside'):
    m=Machine(); w,es=m.page_list(3); m.release()
    target=es[0] if target_kind=='same' else m.entry(es[0]) if target_kind=='nested' else m.node('button')
    m.click(target,gap=100)
    m.advance(300)
    assert m.clicks==[target] and not m.timers,target_kind
    passed()

# A native activation also ends the prior wheel gesture when no pointer-down was delivered.
for home in (False,True):
    m=Machine(); w,es=m.page_list(40,extent=1920)
    if home:
        m.nodes[m.top]['name']='home_page'; m.nodes[w]['type']='slide_menu'
        m.word(w+O['SLIDE_INDEX'],0)
    m.call(gap=0)
    if not home: m.call(gap=50)
    m.click(es[0 if home else 2],gap=50)
    assert m.call(gap=50)==11
    if home: assert slide(m,w)[3]==200
    else: assert m.selected(w)==3
    passed()

# Centre consumes a touch interruption once; its second release must not stop the recall
# glide again and replace the armed row with a different visible row.
m=Machine(); w,es=m.page_list(10); m.paint(w)
for _ in range(5): m.call()
w,es=m.page_list(10); m.touch(); m.glide=False
assert m.release()==11 and m.selected(w)==5
assert m.release(100)==0 and m.screens==[0]
m.advance(300)
assert not m.clicks and not m.timers
passed()

# Rejecting an expired confirmation must not restore or consume the new scope's recall.
for virtual in (False,True):
    m=Machine()
    if virtual: w,rs,es=m.table_page(n=20)
    else: w,es=m.page_list(20,name='allmusic_page')
    off=O['TABLE_TOP'] if virtual else O['SCROLL_Y']
    m.paint(w)
    for _ in range(12): m.call()
    m.word(syms['g_class_type'],2); m.word(w+off,0); m.paint(w); m.release()
    before=dict(m.nodes[w])
    m.word(syms['g_class_type'],0xf001); m.advance(300)
    assert not m.clicks and not m.moved() and m.get(w+off)==0
    assert m.nodes[w]==before, 'rejected confirmation changed live selection properties'
    m.paint(w)
    assert m.selected(w)==12 and m.get(w+off)==540
    passed()

# Touch mode survives settling, unsupported pages, recreation and native clicks anywhere.
for virtual in (False,True):
    for click_only in (False,True):
        m=Machine()
        w,rs=m.list_surface(virtual)
        m.paint(w); assert m.drawn()
        if click_only: m.click(m.node('button'))
        else: m.touch()
        assert any(c[0]=='widget_invalidate_force' and c[1]==m.top for c in m.calls)
        off=O['TABLE_TOP'] if virtual else O['SCROLL_Y']
        anim=O['TABLE_ANIMATOR'] if virtual else O['VIEW_ANIMATOR']
        m.word(w+off,110)
        if virtual: m.bind(rs,110)
        m.word(w+anim,0x1234)
        m.paint(w); assert not m.drawn()
        m.word(w+anim,0); m.paint(w)
        assert m.selected(w)==3 and not m.drawn()
        old=m.top; m.page('playing_page'); m.touch(); m.call()
        m.top=old; m.paint(w); assert not m.drawn()
        # Returning to a recreated list preserves remembered selection, still without drawing.
        w,rs=m.list_surface(virtual)
        m.paint(w); assert m.selected(w)==3 and not m.drawn()
        m.confirm(); m.paint(w); assert m.drawn()  # centre-generated click stays visible
        m.touch(); m.pressed=1; m.call(); m.paint(w)
        assert not m.drawn()  # rejected wheel does not restore drawing
        m.pressed=0; m.call(); m.paint(w); assert m.drawn()
        passed()
# Boundary wheel turns restore drawing and cancel momentum even without selection movement.
for virtual in (False,True):
    m=Machine()
    w,rs=m.list_surface(virtual,rebind=False)
    m.paint(w); m.touch()
    anim=O['TABLE_ANIMATOR'] if virtual else O['VIEW_ANIMATOR']
    m.word(w+anim,0x1234)
    assert m.call(O['KEY_PREV'])==11 and m.selected(w)==0 and m.get(w+anim)==0
    m.paint(w); assert m.drawn()
    passed()
# Touch on a page without a supported pane also hides the next page's outline.
m=Machine(); m.page('playing_page'); m.touch(); w,es=m.page_list(3)
m.paint(w); assert not m.drawn()
m.call(O['KEY_PLAY']); m.paint(w); assert not m.drawn()
m.call(); m.paint(w); assert m.drawn(); passed()

# Immediate table rebinding replaces widgets, not just their indices. Centre resolves new rows.
m=Machine(); w,rs,es=m.table_page(); m.glide=False
old=list(es)
def replace_pool(a,offset):
    global rs,es
    rs=[m.node('table_row') for _ in range(4)]
    es=[m.entry(r) for r in rs]
    m.nodes[a]['children']=rs
    for r,e in zip(rs,es):
        m.nodes[r]['children']=[e]; m.word(r+O['W_PARENT'],a)
    m.bind(rs,offset)
    for e in old: m.nodes.pop(e,None)
m.rebind=replace_pool
m.word(w+O['TABLE_ANIMATOR'],0x1234)
m.call(); m.call()
assert m.selected(w)==2 and m.get(w+O['TABLE_TOP'])==60
assert m.get(w+O['TABLE_ANIMATOR'])==0
m.confirm(); assert m.clicks==[es[1]] and m.clicks[0] not in old
passed()

# Ring lists bump at both ends and hard-stop while the wheel keeps turning; the first
# same-direction detent after a pause wraps to the other end.
m,w,es=walk(6,5,height=96,extent=288,name='playlist_page'); assert m.get(w+O['SCROLL_Y'])==192
assert m.call(gap=100)==11 and m.selected(w)==5 and m.get(w+O['SCROLL_Y'])==192
m.paint(w,gap=0)
assert m.sel()==(0,42,240,48)   # bumped up against the end
for _ in range(3): assert m.call(gap=100)==11 and m.selected(w)==5   # still spinning: hard stop
assert m.call(gap=O['EDGE_PAUSE_MS'])==11 and m.selected(w)==0 and m.get(w+O['SCROLL_Y'])==0
m.paint(w,gap=0); assert m.sel()==(0,0,240,48)
assert m.call(O['KEY_PREV'],gap=100)==11 and m.selected(w)==0
m.paint(w,gap=0); assert m.sel()==(0,6,240,48)   # bumped down at the top
assert m.call(O['KEY_PREV'],gap=100)==11 and m.selected(w)==0
assert m.call(O['KEY_PREV'],gap=O['EDGE_PAUSE_MS'])==11 and m.selected(w)==5 and m.get(w+O['SCROLL_Y'])==192
m.paint(w,gap=0); assert m.sel()==(0,48,240,48)
passed()

# The pause is measured from the last stopped detent, to the millisecond.
m=Machine(); w,es=m.page_list(6,height=96,extent=288,name='folder_page')
m.folder('/sd/albums')
m.paint(w)
for _ in range(5): assert m.call()==11
assert m.call(gap=100)==11 and m.selected(w)==5
assert m.call(gap=O['EDGE_PAUSE_MS']-1)==11 and m.selected(w)==5
m.paint(w,gap=0); assert m.sel()==(0,42,240,48)
assert m.call(gap=O['EDGE_PAUSE_MS']-1)==11 and m.selected(w)==5   # re-armed, not wrapped
assert m.call(gap=O['EDGE_PAUSE_MS'])==11 and m.selected(w)==0
passed()

# Virtual music tables carry over too; the wrap lands on the first logical row.
m=Machine(); w,rs,es=m.table_page(n=4,rebind=True)
m.word(w+O['W_H'],96); m.word(w+O['TABLE_ROWS'],20)
m.paint(w)
for _ in range(19): assert m.call()==11
assert m.selected(w)==19 and m.get(w+O['TABLE_TOP'])==864
assert m.call(gap=100)==11 and m.selected(w)==19
assert m.call(gap=O['EDGE_PAUSE_MS'])==11 and m.selected(w)==0 and m.get(w+O['TABLE_TOP'])==0
assert any(c[0]=='table_client_set_yoffset' for c in m.calls)
passed()

# Settings menus, dynamic pages and the album grid keep hard ends; one-row lists never wrap.
for name in ('sysset_page','playerqueue_page','album_page'):
    m,w,es=walk(6,5,height=96,extent=288,name=name)
    assert m.call(gap=100)==11 and m.selected(w)==5
    m.paint(w,gap=0); assert m.sel()==(0,48,240,48)
    assert m.call(gap=100)==11 and m.selected(w)==5
    passed()
m=Machine(); w,es=m.page_list(1,height=96,extent=96,name='playlist_page')
m.paint(w)
for _ in range(3): assert m.call(gap=100)==11 and m.selected(w)==0
m.paint(w,gap=0); assert m.sel()==(0,0,240,48)
passed()

# Native scrollbar lifecycle, including a fully transparent/invisible mobile bar.
for kind in ('scroll_view','table_client'):
    m=Machine()
    w,es=m.page_list(20,extent=960) if kind=='scroll_view' else (lambda t:(t[0],t[2]))(m.table_page(20))
    bar=m.node('scroll_bar_m',visible=0); m.byte(bar+0x8d,1); m.word(bar+O['BAR_VALUE'],17)
    parent=m.node('list_view' if kind=='scroll_view' else 'table_view',children=[w,bar])
    m.word(w+O['W_PARENT'],parent); m.nodes[m.top]['children']=[parent]
    m.handlers.pop(syms['scroll_bar_scroll_to'])
    m.mock('scroll_bar_cast','scroll_bar_is_mobile','widget_set_opacity','widget_set_sensitive',
           'widget_set_visible_only','widget_animator_prop_create','widget_animator_prop_set_params')
    m.paint(w); m.call(gap=0)
    assert m.nodes[bar]['visible'] and m.u.mem_read(bar+0x34,1)==b'\xff'
    animator=m.get(bar+0x98)
    assert animator and m.get(animator+O['ANIM_DURATION'])==500 and m.get(animator+0x24)==300
    assert m.get(bar+O['BAR_VALUE'])==17  # waking must not calculate or move the thumb
    m.touch(); m.call(gap=0)
    assert m.get(bar+0x98)!=animator
    assert any(c[0]=='widget_animator_destroy' and c[1]==animator for c in m.calls)
    # The stock opacity completion disables and hides the bar; no payload fade timer exists.
    animator=m.get(bar+0x98); callback,ctx=m.slide_callbacks[animator]
    m.byte(bar+0x34,0)
    assert m.call(address=callback,args=(ctx,0,0,0),gap=0)==7
    assert not m.nodes[bar]['visible'] and not m.nodes[bar]['sensitive'] and not m.get(bar+0x98)
    m.call(gap=0); assert m.nodes[bar]['visible']
    m.top=0; m.nodes.clear(); m.advance(1000)  # payload retains no native bar/animator pointer
    passed()
for count,bar_present in ((2,True),(20,False)):
    m=Machine(); w,es=m.page_list(count,height=96,extent=count*48)
    parent=m.node('list_view',children=[w]+([m.node('scroll_bar_m')] if bar_present else []))
    m.word(w+O['W_PARENT'],parent); m.paint(w); m.call()
    assert not any(c[0]=='scroll_bar_scroll_to' for c in m.calls)
    passed()

# Bump repaints at precisely 120 ms, independently of the pause-to-wrap arm.
m=Machine(); w,es=m.page_list(2,height=96,extent=96,name='playlist_page')
m.paint(w); m.call(); m.call(gap=0)
assert m.timers and min(t[0] for t in m.timers.values())==m.now+O['BUMP_MS']
m.advance(O['BUMP_MS']-1); assert not m.calls
m.advance(1); assert any(c[0]=='widget_invalidate_force' and c[1]==w for c in m.calls)
m.paint(w,gap=0); assert m.sel()==(0,48,240,48)
m.call(gap=O['EDGE_PAUSE_MS']); assert m.selected(w)==0
passed()
for cancel in ('touch','activate','recycle','destroy','scope'):
    m=Machine(); w,es=m.page_list(2,height=96,extent=96,name='folder_page')
    m.folder('/sd/a'); m.paint(w); m.call(); m.call(gap=0)
    if cancel=='touch': m.touch()
    elif cancel=='activate': m.click(es[-1])
    elif cancel=='recycle': m.nodes[w].pop('_ringnav_fx')
    elif cancel=='destroy': m.top=0; m.nodes.clear()
    else: m.folder('/sd/b'); m.paint(w,gap=0)
    m.advance(O['BUMP_MS'])
    assert not any(c[0]=='widget_invalidate_force' and c[1]==w for c in m.calls)
    passed()

# Execute stock navigator, Now Playing initialization/key-up, and native window switch/close.
# Resource loading, array storage, paint and animation services remain mocked.
class NavigationMachine(Machine):
    def __init__(self):
        super().__init__()
        self.order=[]; self.block_open=False
        self.word(self.wm+0x94,0x9a0490)
        for name in ('navigator_switch_to_with_context','navigator_back_to_home','navigator_to_with_context',
                     'navigator_to','window_close'):
            self.handlers.pop(syms[name],None)
        self.mock('widget_child','window_open_and_close','widget_restack','widget_get_window',
                  'widget_is_keyboard','widget_is_dialog','widget_is_window','widget_is_normal_window',
                  'widget_remove_child','widget_destroy','window_manager_dispatch_window_event',
                  'widget_foreach','widget_on','playing_timer_start','access@GLIBC_2.0',
                  'remove@GLIBC_2.0','idle_add','netdisk_folder_clear','awake_screen',prefix='nav:')
        self.handlers[syms['strcmp@GLIBC_2.0']]='tk_strcmp'
        for address in (0x52b6f4,0x68541c,0x6885c0,0x6861d4): self.handlers[address]='nav:'+hex(address)
    def sync_order(self):
        self.top=self.order[-1] if self.order else 0
        arr=self.alloc(12); items=self.alloc(max(4,len(self.order)*4))
        self.word(self.wm+0x5c,arr); self.word(arr,len(self.order)); self.word(arr+8,items)
        for i,w in enumerate(self.order): self.word(items+4*i,w); self.word(w+O['W_PARENT'],self.wm)
    def hook(self,u,address,size,unused):
        name=self.handlers.get(address,'')
        if not name.startswith('nav:'): return super().hook(u,address,size,unused)
        assert u.reg_read(UC_MIPS_REG_T9)==address
        name=name[4:]; a,b,c,d=[u.reg_read(r) for r in REGS]; ret=0
        self.calls.append((name,a,b,c))
        if name=='widget_child':ret=next((w for w in self.order if self.nodes[w]['name']==self.text(b)),0)
        elif name=='window_open_and_close':
            if not self.block_open:
                ret=self.node('window',self.text(a),stage=3); self.order.append(ret); self.sync_order()
        elif name=='widget_restack':
            self.order.remove(a); self.order.insert(min(b,len(self.order)),a); self.sync_order()
        elif name=='widget_get_window': ret=a
        elif name in ('widget_is_window','widget_is_normal_window'): ret=int(a in self.order)
        elif name=='widget_remove_child': self.order.remove(b); self.sync_order()
        elif name=='0x6885c0': ret=1  # stock no-window-animation completion branch
        elif name=='0x6861d4':ret=self.top
        elif name=='access@GLIBC_2.0':ret=-1
        u.reg_write(UC_MIPS_REG_V0,ret&0xffffffff); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))

if variant=='ipod':
    for existing in (False,True):
        for empty in (False,True):
            m=NavigationMachine(); w,es=m.page_list(20,name='folder_page'); browse=m.top
            home=m.node('window','home_page',stage=3)
            playing=m.node('window','playing_page',stage=3) if existing else 0
            m.order=[home]+([playing] if playing else [])+[browse]; m.sync_order()
            m.folder('/sd/music'); m.paint(w)
            for _ in range(6):m.call()
            selected,offset=m.selected(w),m.get(w+O['SCROLL_Y'])
            for visit in range(3):
                long_return(m)
                assert m.nodes[m.top]['name']=='playing_page'
                assert not any(c[0]=='player_start' for c in m.calls)
                # The switch dropped the hold-release latch: the hold's release is ignored
                # and the first short Return goes back instead of being swallowed.
                assert m.u.mem_read(O['RETURN_RELEASE_LATCH'],1)==b'\0'
                assert m.call(170,gap=0)==0 and m.nodes[m.top]['name']=='playing_page'
                m.call(170,address=0x52c248,args=(m.top,m.event,0,0),gap=0)
                assert m.top==browse, (existing,visit,[m.nodes[p]['name'] for p in m.order])
                m.paint(w,gap=0); assert (m.selected(w),m.get(w+O['SCROLL_Y']))==(selected,offset)
            passed()
    m=NavigationMachine(); m.page('folder_page'); browse=m.top; m.order=[browse]; m.sync_order()
    m.block_open=True; long_return(m); assert m.top==browse
    assert m.call(170,gap=0)==11
    m.block_open=False; m.animating=1; long_return(m); assert m.top==browse
    passed()

# Pull-to-search observes the page's native before-children callbacks and stock dispatch.
class PullMachine(Machine):
    def child(self,page,name):
        def walk(w):
            if self.nodes[w]['name']==name:return w
            for child in self.nodes[w]['children']:
                found=walk(child)
                if found:return found
            return 0
        return walk(page)
    def emit(self,w,typ,x=100,y=90):
        self.word(self.event+O['EVENT_TARGET'],w)
        self.word(self.event+O['EVENT_Y'],y)
        ret=0
        for t,callback,ctx in list(self.nodes[w].get('handlers',[])):
            if t==typ:
                ret=self.call(key=x,address=callback,args=(ctx,self.event,0,0),event_type=typ,gap=0,clear=False)
                if ret==11:break
        return ret
    def start_pull(self,page,w,x=100,y=60):
        self.top=page; self.word(w+O['W_PARENT'],page);self.word(page+O['W_Y'],30)
        self.word(self.event+O['EVENT_Y'],y)
        return self.call(key=x,address=HOOKS['on_wm_tsdown_before_fun'][0],event_type=O['EVT_POINTER_DOWN'],gap=0)
    def prompt(self,page):
        w=self.child(page,'_pull_prompt')
        return self.nodes[w] if w else {'visible':0}
    def searches(self):return [c for c in self.calls if c[0]=='stock_search']

# Stock constructors/dispatch establish these event numbers and fields, independently of mocks.
m=Machine()
for n in ('pointer_event_init','event_init'):m.handlers.pop(syms[n],None)
p=m.alloc(48);m.call(address=syms['pointer_event_init'],args=(p,O['EVT_POINTER_DOWN'],m.wm,37),stack=(91,))
assert m.get(p+O['EVENT_TARGET'])==m.wm and m.get(p+O['EVENT_X'])==37 and m.get(p+O['EVENT_Y'])==91
assert m.get(p+4)==48;passed()

# The stock wm registration fixes the type that reaches the hooked down callback. The
# move-before thunk forwards 0x102; 0xff belongs to the separate stock on_wm_tsdown_fun.
def wm_event_type(address):
    return struct.unpack('<I',m.u.mem_read(address,4))[0]&0xffff
assert wm_event_type(0x523ef0)==O['EVT_POINTER_DOWN']==0x100
assert wm_event_type(0x523ed4)==O['EVT_POINTER_MOVE_BEFORE']==0x102
assert wm_event_type(0x523f28)==0xff
assert wm_event_type(0x523e6c)==O['EVT_KEY_DOWN_BEFORE']==0x112
assert wm_event_type(0x523eb8)==O['EVT_KEY_UP']==0x114
assert wm_event_type(0x523e80)==O['EVT_KEY_UP_BEFORE']==0x115
passed()

for page_name in ('folder_page','localmusic_page','allmusic_page'):
    for empty in (False,True):
        for distance in (0,7,8,47,48,49):
            m=PullMachine()
            if page_name=='folder_page':
                w,rows,es=m.table_page(name=page_name)
                if empty:m.word(w+O['TABLE_ROWS'],0);m.nodes[w]['children']=[]
            else:w,es=m.page_list(0 if empty else 5,name=page_name)
            page=m.top;m.start_pull(page,w)
            m.emit(page,O['EVT_POINTER_MOVE_BEFORE'],y=60+distance)
            enabled=variant=='ipod' and page_name=='localmusic_page'
            assert bool(m.prompt(page)['visible'])==(enabled and distance>=8)
            if enabled and distance>=8:
                assert m.prompt(page)['text']==('Release to search' if distance>=48 else 'Pull to search')
                assert m.click(es[0])==11 if es else True
            # click() normally uses a one-second gap; gesture state still belongs to the page.
            m.emit(page,O['EVT_POINTER_UP_BEFORE'],y=60+distance)
            assert len(m.searches())==(1 if enabled and distance>=48 else 0)
            m.emit(page,O['EVT_POINTER_UP_BEFORE'],y=120)
            assert len(m.searches())==(1 if enabled and distance>=48 else 0)
            assert not m.prompt(page)['visible']
            passed()

if variant=='ipod':
    # Local Songs' virtual table keeps its rows and top offset across a search.
    m=PullMachine();w,rows,es=m.table_page(name='localmusic_page');page=m.top
    m.start_pull(page,w);count=m.get(w+O['TABLE_ROWS'])
    m.emit(page,O['EVT_POINTER_MOVE_BEFORE'],y=120);m.emit(page,O['EVT_POINTER_UP_BEFORE'],y=120)
    assert len(m.searches())==1 and m.get(w+O['TABLE_ROWS'])==count and m.get(w+O['TABLE_TOP'])==0
    # Stock search opens a dialog on this page; resuming it needs no index translation.
    m.start_pull(page,w);m.emit(page,O['EVT_POINTER_UP_BEFORE'],y=60)
    assert m.click(es[0])==0
    passed()
    for cancel in ('below','horizontal','upward','midlist','outside','edge0','edge30','edge31',
                   'wheel','button','screen','navigate','background','close','destroy','surface','abort','move_hook'):
        m=PullMachine();w,es=m.page_list(8,height=240,name='localmusic_page');page=m.top
        y=30 if cancel=='edge30' else 0 if cancel=='edge0' else 31 if cancel=='edge31' else 60
        if cancel=='midlist':m.word(w+O['SCROLL_Y'],20)
        m.start_pull(page,w,x=250 if cancel=='outside' else 100,y=y)
        if cancel=='horizontal':m.emit(page,O['EVT_POINTER_MOVE_BEFORE'],x=170,y=y+20)
        elif cancel=='upward':m.emit(page,O['EVT_POINTER_MOVE_BEFORE'],y=y-10)
        m.emit(page,O['EVT_POINTER_MOVE_BEFORE'],y=y+60)
        if cancel=='below':m.emit(page,O['EVT_POINTER_MOVE_BEFORE'],y=y+47)
        elif cancel in ('wheel','button'):m.call(O['KEY_NEXT'] if cancel=='wheel' else O['KEY_PLAY'],gap=0)
        elif cancel=='screen':m.byte(syms['g_backlight_status'],0);m.paint(w,gap=0)
        elif cancel=='navigate':m.page('home_page');m.paint(m.top,gap=0)
        elif cancel in ('background','close','destroy','surface','abort'):
            typ=O[{'background':'EVT_WINDOW_BACKGROUND','close':'EVT_WINDOW_CLOSE','destroy':'EVT_DESTROY',
                   'surface':'EVT_DESTROY','abort':'EVT_POINTER_ABORT'}[cancel]]
            m.emit(w if cancel=='surface' else page,typ)
        elif cancel=='move_hook':
            # The native wm move-before (0x102) tail-calls the down-before hook (0x100).
            # The move type must not restart the pull.
            m.word(m.event+O['EVENT_Y'],y+60)
            m.call(key=100,address=syms['on_wm_tsmove_before_fun'],event_type=O['EVT_POINTER_MOVE_BEFORE'],gap=0,clear=False)
        m.emit(page,O['EVT_POINTER_UP_BEFORE'],y=y+(47 if cancel=='below' else 60))
        assert len(m.searches())==(1 if cancel in ('edge31','move_hook') else 0),cancel
        assert not m.prompt(page)['visible'],cancel
        passed()

# Native before-children dispatch invokes the page callback before it can activate a row.
class PullDispatchMachine(PullMachine):
    def __init__(self):
        super().__init__()
        self.mock('widget_ref','widget_unref','widget_on_pointer_move_children','widget_on_pointer_up_children',
                  'widget_on_event_before_children','widget_vtable_on_pointer_move','widget_vtable_on_pointer_up','emitter_dispatch',prefix='input:')
    def hook(self,u,address,size,unused):
        name=self.handlers.get(address,'')
        if not name.startswith('input:'):return super().hook(u,address,size,unused)
        assert u.reg_read(UC_MIPS_REG_T9)==address
        a,b=[u.reg_read(r) for r in REGS[:2]];self.calls.append((name,a,b,0))
        if name=='input:emitter_dispatch':
            matches=[(cb,ctx) for typ,cb,ctx in self.nodes[a].get('handlers',[]) if typ==self.get(b)]
            assert len(matches)<=1
            if matches:
                cb,ctx=matches[0];u.reg_write(UC_MIPS_REG_A0,ctx)
                u.reg_write(UC_MIPS_REG_T9,cb);u.reg_write(UC_MIPS_REG_PC,cb);return
        u.reg_write(UC_MIPS_REG_V0,0);u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))

if variant=='ipod':
    for distance in (7,48):
        m=PullDispatchMachine();w,es=m.page_list(5,height=240,name='localmusic_page');page=m.top
        m.start_pull(page,w)
        m.word(page+0x74,1);m.word(page+O['W_EMITTER'],page)
        m.word(m.event+O['EVENT_Y'],60+distance)
        for fn,typ in (('widget_on_pointer_move',0x101),('widget_on_pointer_up',0x103)):
            m.calls=[]
            ret=m.call(key=100,address=syms[fn],args=(page,m.event,0,0),event_type=typ,gap=0,clear=False)
            child_calls=[c for c in m.calls if c[0]=='input:'+fn+'_children']
            assert (ret==11 and not child_calls) if distance>=8 else bool(child_calls)
        assert len(m.searches())==(distance>=48)
        passed()
    # Native abort delivery follows the pressed target chain; it is not an EVT_CLICK.
    m=PullMachine();w,es=m.page_list(3,height=240,name='localmusic_page');page=m.top
    m.start_pull(page,w);m.handlers.pop(syms['widget_dispatch_event_to_target_recursive'])
    m.word(page+0x4c,w);m.word(w+0x4c,es[0])
    m.calls=[];m.emit(page,O['EVT_POINTER_MOVE_BEFORE'],y=120)
    assert [c[1] for c in m.dispatched()]==[w,es[0]] and not m.clicks
    m.emit(page,O['EVT_POINTER_UP_BEFORE'],y=100);assert not m.searches()
    assert m.click(es[0])==11
    passed()

    # Use the stock search callback, navigator and Back path. Only resource initialization
    # and window storage are mocked, as in the Now Playing navigation cases above.
    class SearchNavigationMachine(PullMachine,NavigationMachine):
        def __init__(self):
            super().__init__()
            self.handlers.pop(syms['stock_search'],None)
            self.handlers.pop(syms['navigator_to'],None)
            self.mock('dialog_search_dialog_init',prefix='nav:'); self.mock('widget_on')
    m=SearchNavigationMachine();w,es=m.page_list(6,height=240,name='localmusic_page');page=m.top
    m.order=[page];m.sync_order()
    m.start_pull(page,w);m.emit(page,O['EVT_POINTER_MOVE_BEFORE'],y=120)
    m.emit(page,O['EVT_POINTER_UP_BEFORE'],y=120)
    assert m.nodes[m.top]['name']=='dialog/search_dialog' and m.order[0]==page
    # The stock dialog owns its Back handling and its callbacks are outside this harness;
    # model the window manager popping it. The Local Songs page must still be the same
    # instance with its offset and rows untouched.
    m.order.remove(m.top);m.sync_order()
    assert m.top==page
    assert m.get(w+O['SCROLL_Y'])==0 and len(m.nodes[w]['children'])==6
    passed()

# Artist detail opens on Albums: the real init reaches the stock Albums tab handler, which
# selects tab 1 and queries albums before building; stock builds Songs instead.
for patched in (True, False):
    m=Machine(patched)
    for address,name in ((0x4adcbc,'songs_view'),(0x4ac084,'albums_view'),(0x4ad4c8,'timer_idex'),(0x4ed0c4,'query_run')):
        m.handlers[address]=name
    m.mock('load_artistinfo_list','load_localartist_list','widget_get_window','widget_foreach','strcpy@GLIBC_2.0')
    arg=m.alloc(8); m.word(arg+4,m.alloc(0x40))
    assert m.call(address=syms['localmusic_artistinfo_page_init'],args=(m.node('window','artistinfo_page'),arg,0,0))==0
    names=[c[0] for c in m.calls if c[0] in ('songs_view','albums_view','load_localartist_list','query_run')]
    tab=m.get(m.get(0xa26cc0-0x5810)+0x7678)
    if patched:
        assert names==['query_run','load_localartist_list','query_run','albums_view'] and tab==1, names
        assert [c[1] for c in m.calls if c[0]=='load_localartist_list']==[1]
    else: assert names==['query_run','songs_view'] and tab==0, names
    passed()

# Centre hold queue menu. libcstl deques are Python lists of element addresses; the stock
# mclLoadPlayList, mclNextSong and key filters run for real.
from build import SHUFFLE_CALL, fileoff
class QueueMachine(Machine):
    def __init__(self,page='allmusic_page',rows=20,queue=3,pos=0,mode=0,cls=0xf001):
        super().__init__()
        self.deqs={}; self.toasts=[]; self.sent=[]; self.picks=[]; self.airplay=0
        for n in ('_create_deque','deque_init','deque_init_copy','deque_size','deque_at','_deque_push_back',
                  'deque_assign','deque_clear','deque_destroy','deque_pop_back','send@GLIBC_2.0','window_manager_get_input_device_status',
                  'navigator_to','navigator_to_with_context','navigator_switch_to_with_context','window_close','widget_on','widget_destroy_children',
                  'getMusicByAlbum','getMusicByAlbumAndSonger','getMusicByAlbumAndAlbumSonger','toolsLoadDirectory',
                  'getMusicBySonger','getMusicByAlbumArtist','getMusicByComposer','getMusicByGenre',
                  'getMusicByAlbumAndComposer','getMusicByAlbumAndGenre','checkFavExist','navigator_window_is_exist',
                  'mcl_shuffle_pick','airplayGetFlag'): self.handlers[syms[n]]='q:'+n
        self.favs=set(); self.open_pages=set(); self.opened=[]
        self.handlers.pop(syms['mclLoadPlayList'])
        self.mock('strlen@GLIBC_2.0','snprintf@GLIBC_2.0'); self.handlers[syms['strcmp@GLIBC_2.0']]='tk_strcmp'
        self.mock('mclStartPlayer','mclStop','mclSetPause','mclSetResume','mclSetSeek')
        self.status=self.alloc(0x200); self.word(syms['g_class_type'],cls)
        self.surface,_,_=self.table_page(4,page,rebind=True); self.word(self.surface+O['TABLE_ROWS'],rows)
        self.word(syms['p_deque_showlist'],self.deque([self.song(f'Row {i}') for i in range(rows)]))
        self.word(syms['mcl_pdeqplaylist'],self.deque([self.song(c) for c in 'ABC'[:queue]]))
        self.word(syms['tools_pdeq_directory'],self.deque([self.song('staged')]))
        self.word(O['MCL_POOL'],self.deque(list(range(queue)),'int'))
        for k,v in (('MCL_POS',pos),('MCL_MODE',mode),('MCL_TYPE',cls),('MCL_LASTPOS',-1),('MCL_PRELOAD',0),('MCL_FD',-1)): self.word(O[k],v)
        self.found=[self.song('T1'),self.song('dir',4),self.song('T2')]; self.stack=[self.top]
        self.folder('/mnt/sd/Music'); self.paint(self.surface)
    def song(self,name,kind=8):
        r=self.alloc(0x60)
        for off,v in (('REC_ID',1),('REC_NAME',self.string(name)),('REC_PATH',self.string('/p/'+name)),
                      ('REC_ALBUM',self.string('Album')),('REC_ARTIST',self.string('Artist')),('REC_TYPE',kind)): self.word(r+O[off],v)
        return r
    def deque(self,items,kind='stSongInfo'):
        h=self.alloc(0x20); self.deqs[h]=[kind,[self.copy(kind,e) for e in items]]; return h
    def copy(self,kind,e):
        a=self.alloc(0x60); self.u.mem_write(a,struct.pack('<I',e&0xffffffff) if kind=='int' else bytes(self.u.mem_read(e,0x60)))
        return a
    def items(self,h): return self.deqs[h][1]
    def row(self,i): return self.items(self.get(syms['p_deque_showlist']))[i]
    def names(self,h=None): return [self.text(self.get(e+O['REC_NAME'])) for e in self.items(h or self.get(syms['mcl_pdeqplaylist']))]
    def pool(self): return [signed(self.get(e)) for e in self.items(self.get(O['MCL_POOL']))]
    def mcl(self,k): return signed(self.get(O[k]))
    def dialog(self):
        self.view=self.node('scroll_view','scroll_view',[self.node('list_item') for _ in range(5)])
        self.title=self.node('hscroll_label','scrlabel_title'); self.back=self.node('image','img_return')
        return self.node('dialog','sortselect_dialog',[self.node('view','view_navbar',[self.title,self.back]),
                                                       self.node('list_view','list_view',[self.view])])
    def hook(self,u,address,size,unused):
        name=self.handlers.get(address,'')
        if not name.startswith('q:'): return super().hook(u,address,size,unused)
        assert u.reg_read(UC_MIPS_REG_T9)==address, name
        name=name[2:]; a,b,c,d=[u.reg_read(r) for r in REGS]; ret=0
        self.calls.append((name,a,b,c))
        if name=='_create_deque': ret=self.alloc(0x20); self.deqs[ret]=[self.text(a),[]]
        elif name in ('deque_init','deque_clear'): self.deqs[a][1]=[]
        elif name in ('deque_init_copy','deque_assign'): self.deqs[a][1]=[self.copy(self.deqs[a][0],e) for e in self.items(b)]
        elif name=='deque_size': ret=len(self.items(a))
        elif name=='deque_at': ret=self.items(a)[b]
        elif name=='_deque_push_back': self.items(a).append(self.copy(self.deqs[a][0],b))
        elif name=='deque_destroy': del self.deqs[a]
        elif name=='deque_pop_back': self.items(a).pop()
        elif name=='send@GLIBC_2.0': self.sent.append((a,bytes(u.mem_read(b,c)),c,d)); ret=c
        elif name=='window_manager_get_input_device_status': ret=self.status
        elif name=='airplayGetFlag': ret=self.airplay
        elif name=='navigator_to':
            page=self.text(a); self.top=self.dialog() if page=='dialog/sortselect_dialog' else self.node('window',page)
            self.stack.append(self.top); self.opened.append((page,None))
        elif name=='navigator_to_with_context':
            page=self.text(a)
            if page=='dialog/msginfo_dialog': self.toasts.append((page,self.get(b),self.get(b+4),self.text(b+8)))
            elif page=='playing_page': self.opened.append((page,self.names(self.get(b)),self.get(b+4),self.get(b+8),self.get(b+12)))
            elif page=='localmusic/playlist_page': self.opened.append((page,b))
            else: self.opened.append((page,self.get(b),self.get(b+4))); self.top=self.node('window',page); self.stack.append(self.top)
        elif name=='navigator_switch_to_with_context':
            self.opened.append((self.text(a),*[self.get(b+4*i) for i in range(4)]))
        elif name=='checkFavExist': ret=self.text(self.get(a+O['REC_NAME'])) in self.favs
        elif name=='navigator_window_is_exist': ret=self.text(a) in self.open_pages
        elif name=='window_close': self.stack.remove(a); self.top=self.stack[-1]
        elif name=='widget_destroy_children': self.nodes[a]['children']=[]
        elif name=='widget_on':
            self.nodes[a].setdefault('handlers',[]).append((b,c,d)); ret=1
            if b==O['EVT_CLICK']:
                em=self.alloc(4); it=self.alloc(0x28); self.word(a+O['W_EMITTER'],em); self.word(em,it); self.word(it+O['EMIT_TYPE'],b)
        elif name=='mcl_shuffle_pick': self.picks.append(a); self.word(O['MCL_POS'],len(self.names())-1); ret=1
        else:  # the album/folder queries fill the staging deque
            self.query=(name,self.text(a) if a else None,(self.text(b) if b else None) if 'And' in name else None,c)
            self.deqs[self.get(syms['tools_pdeq_directory'])][1]=[self.copy('stSongInfo',e) for e in self.found]; ret=3
        for r in [UC_MIPS_REG_V1,*REGS,UC_MIPS_REG_T8,UC_MIPS_REG_T9]: u.reg_write(r,0xdeadbeef)
        u.reg_write(UC_MIPS_REG_V0,ret&0xffffffff); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))
    def press(self,t,key=O['KEY_CENTER']):
        self.word(self.status+O['INPUT_KEYS'],key); self.word(self.status+O['INPUT_KEYS']+O['INPUT_KEY_TIME'],t)
    def hold(self,key=O['KEY_CENTER']):
        ret=self.call(key,address=syms['on_wm_keylong_fun'],event_type=O['EVT_KEY_LONG'],gap=0)
        self.advance(0,clear=False)
        return ret
    def release(self,key=O['KEY_CENTER']):
        """Hook, then the real stock key-up; AWTK clears the key record afterwards."""
        if self.call(key,gap=0)==0: self.call(key,address=syms['on_wm_keyup_fun'],gap=0,clear=False)
        self.u.mem_write(self.status+O['INPUT_KEYS'],bytes(O['INPUT_KEY_SIZE']))
        return sum(c[0]=='playpause_quick_click' for c in self.calls)
    def handler(self,w,kind): return next((f,ctx) for t,f,ctx in self.nodes[w]['handlers'] if t==kind)
    def pick(self,row):
        f,ctx=self.handler(self.nodes[self.view]['children'][row],O['EVT_CLICK'])
        assert self.call(address=f,args=(ctx,self.event,0,0),gap=0)==0
    def run(self,row,steps=0):
        """Wheel down steps rows, hold, pick a menu row and let the deferred action run."""
        for _ in range(steps): self.call()
        n=len(self.toasts); self.t=getattr(self,"t",7000)+1; self.press(self.t); assert self.hold()==11 and self.release()==0
        self.pick(row); self.advance(0)
        return self.toasts[-1][3] if len(self.toasts)>n else None
    def labels(self): return [self.nodes[self.nodes[i]['children'][0]]['text'] for i in self.nodes[self.view]['children']]
    def playback(self): return [c for c in self.calls if c[0] in ('mclStartPlayer','mclStop','mclSetPause','mclSetResume','mclSetSeek','playpause_quick_click')]

SONG_MENU=['Play next','Add to queue','Add to Favourites','Add to playlist','Go to album','Go to artist']
# A centre hold opens one menu titled by its row. Its release is swallowed, and a repeated long
# event of the same press opens nothing.
m=QueueMachine(); page=m.top
m.press(6000); assert m.hold()==11 and m.nodes[m.top]['name']=='sortselect_dialog'
assert {c[1:] for c in m.calls if c[0]=='widget_off_by_func'}=={(m.top,O['EVT_KEY_UP'],O['SORTSELECT_KEYUP']),(m.back,O['EVT_CLICK'],O['SORTSELECT_CLOSE'])}
assert m.nodes[m.title]['text']=='Row 0' and m.labels()==SONG_MENU
assert m.hold()==11 and not any(c[0]=='navigator_to' for c in m.calls) and len(m.stack)==2
assert m.release()==0
passed()
# Centre holds without row actions never enter stock's shutdown confirmation. Their releases
# are swallowed too, including on Now Playing and when the screen is off or locked.
for page,flags in (('playing_page',{}),('sysset_page',{}),('home_page',{'g_backlight_status':0}),
                   ('poweroff_page',{'g_lockscreen_pageflag':1})):
    m=QueueMachine(page=page)
    for flag,value in flags.items(): m.byte(syms[flag],value)
    m.press(8000)
    assert m.hold()==11 and not m.opened
    assert m.hold()==11 and not m.opened
    assert m.release()==0 and not m.screens and not m.opened
    assert m.u.mem_read(syms['g_poweroff_state'],1)==b'\0'
    assert m.u.mem_read(syms['g_power_longkey'],1)==b'\0'; passed()
# A Play/Pause hold opens Now Playing without restarting playback, and its release does not toggle.
m=QueueMachine(); m.press(7000,O['KEY_PLAY'])
assert m.hold(O['KEY_PLAY'])==11 and m.opened[-1]==('playing_page',0,0,255,2)
assert m.release(O['KEY_PLAY'])==0 and not m.playback(); passed()
# Return (the replaced dialog key-up) dismisses without the stock sort flag; other keys pass.
f,ctx=m.handler(m.top,O['EVT_KEY_UP']); ev=m.alloc(0x40); m.word(ev,O['EVT_KEY_UP']); m.word(ev+O['EVENT_KEY'],O['KEY_NEXT'])
assert m.call(address=f,args=(ctx,ev,0,0),gap=0)==0 and m.top!=page
m.word(ev+O['EVENT_KEY'],O['KEY_RETURN']); assert m.call(address=f,args=(ctx,ev,0,0),gap=0)==11
assert m.top==page and not m.toasts and m.names()==['A','B','C'] and m.u.mem_read(syms['g_sort_changeflag'],1)==b'\0'
passed()
# A dropped release leaves a latch that must not eat a later press: the press time differs.
m=QueueMachine(); m.press(1); assert m.hold()==11
m.u.mem_write(m.status+O['INPUT_KEYS'],bytes(O['INPUT_KEY_SIZE'])); m.press(2); assert m.release()==0; passed()
# The hold cancels a pending centre click. Wheel and centre then act on the menu only and the list
# keeps its selection. Touch and centre arrive as the same click and run once.
m=QueueMachine(); m.call(); m.call(); assert m.selected(m.surface)==2
m.call(O['KEY_CENTER']); m.press(100); m.hold(); m.release(); m.advance(300); assert not m.dispatched()
m.paint(m.view); assert m.call()==11 and m.selected(m.view)==1 and m.selected(m.surface)==2
assert m.confirm()==11 and m.dispatched()[0][1]==m.nodes[m.view]['children'][1]
m.pick(1); m.pick(1); m.advance(0)
assert m.names()==['A','B','C','Row 2'] and not m.toasts
assert m.top==page and m.selected(m.surface)==2 and not m.playback(); passed()
# Play next lands after the playing track, on the logical row of a recycled table. Later tracks,
# the shuffle pool and the previous index shift; duplicates stay; playback is never touched.
m=QueueMachine(); m.word(O['MCL_LASTPOS'],2)
assert m.run(0,steps=4) is None and m.names()==['A','Row 4','B','C']
assert m.pool()==[0,2,3,1] and m.mcl('MCL_LASTPOS')==3 and m.mcl('MCL_POS')==0
m.run(0); assert m.names()==['A','Row 4','Row 4','B','C'] and not m.playback(); passed()
# Albums and an artist's albums use the stock detail queries; a folder row loads like folder_enter.
# Only songs join, in order, and the staging deque is restored.
for cls,artist_type,page,query in ((0xf003,0,'album_page',('getMusicByAlbum','Album',None)),
        (0xff01,1,'album_page',('getMusicByAlbumAndAlbumSonger','Album','Artist')),
        (0xff01,0,'localclass_page',('getMusicByAlbumAndSonger','Album','Artist')),
        (0xf001,0,'folder_page',('toolsLoadDirectory','/mnt/sd/Music/Row 0',None))):
    m=QueueMachine(page=page,cls=cls,pos=2); m.word(syms['g_artist_type'],artist_type)
    if page=='folder_page': m.word(m.row(0)+O['REC_TYPE'],4)
    assert m.run(1) is None and m.names()==['A','B','C','T1','T2'] and m.query[:3]==query, m.query
    assert m.names(m.get(syms['tools_pdeq_directory']))==['staged'] and m.pool()==[0,1,2,3,4]
    passed()
# Artist, composer and genre rows, and their album lists, use batch_add_file's per-class queries.
for cls,artist_type,query in ((0xf004,0,('getMusicBySonger','Artist',None)),(0xf004,1,('getMusicByAlbumArtist','Artist',None)),
        (0xf005,0,('getMusicByComposer','Comp',None)),(0xf006,0,('getMusicByGenre','Pop',None)),
        (0xff02,0,('getMusicByAlbumAndComposer','Album','Comp')),(0xff03,0,('getMusicByAlbumAndGenre','Album','Pop'))):
    m=QueueMachine(page='localclass_page',cls=cls,pos=2); m.word(syms['g_artist_type'],artist_type)
    m.word(m.row(0)+0x20,m.string('Comp')); m.word(m.row(0)+0x1c,m.string('Pop'))
    assert m.run(1) is None and m.names()==['A','B','C','T1','T2'] and m.query[:3]==query, m.query
    assert m.query[3]==0 or cls<0xff00; passed()
# An Unknown row (id -1) queries the empty name; an artist's All songs row (-2) sets the flag.
m=QueueMachine(page='localclass_page',cls=0xf004); m.word(m.row(0)+O['REC_ID'],-1); m.run(1)
assert m.query[:2]==('getMusicBySonger',None); passed()
m=QueueMachine(page='localclass_page',cls=0xff01); m.word(m.row(0)+O['REC_ID'],-2); m.run(1)
assert m.query==('getMusicByAlbumAndSonger','Album','Artist',1); passed()
# Each row gets the menu that fits it. Songs: favourite as stock's heart, add to a playlist and go
# to album and artist; collections shuffle instead; folders have no favourite; Coverflow (below)
# has no playlist. Go to is left out where it would land on the page itself or a second instance.
def menu(m,setup=None):
    if setup: setup(m)
    m.press(5); assert m.hold()==11; t=m.nodes[m.title]['text']; got=m.labels(); m.release(); return t,got
GROUP=['Play next','Add to queue','Shuffle','Add to playlist']
for kw,setup,want in (({},None,('Row 0',SONG_MENU)),
        ({},lambda m:m.favs.add('Row 0'),('Row 0',[*SONG_MENU[:2],'Remove from Favourites',*SONG_MENU[3:]])),
        ({},lambda m:m.open_pages.update({'artistinfo_page','playerjumpinfo_page'}),('Row 0',SONG_MENU[:4])),
        ({'cls':0xff10,'page':'album_page'},None,('Row 0',[*SONG_MENU[:4],'Go to artist'])),
        ({},lambda m:m.word(m.row(0)+O['REC_ALBUM'],0),('Row 0',[*SONG_MENU[:4],'Go to artist'])),
        ({'cls':0xf003,'page':'album_page'},None,('Album',[*GROUP,'Go to artist'])),
        ({'cls':0xff01,'page':'album_page'},None,('Album',GROUP)),
        # An artist's tabs set classinfo +0, not g_class_type: Albums first, then Songs after an album.
        ({'cls':0xff07,'page':'artistinfo_page'},lambda m:m.word(syms['g_local_classinfo_save'],0xff01),('Album',GROUP)),
        ({'cls':0xff01,'page':'artistinfo_page'},lambda m:(m.word(syms['g_local_classinfo_save'],0xff07),m.open_pages.add('artistinfo_page')),('Row 0',SONG_MENU[:5])),
        ({'cls':0xf004,'page':'localclass_page'},None,('Artist',GROUP)),
        ({'cls':0xf006,'page':'localclass_page'},lambda m:m.word(m.row(0)+0x1c,m.string('Pop')),('Pop',GROUP)),
        ({'page':'folder_page','cls':1},lambda m:m.word(m.row(0)+O['REC_TYPE'],4),('Row 0',GROUP))):
    assert menu(QueueMachine(**kw),setup)==want, (kw,want); passed()
# Favourites: Add runs batch-select's Add to My Fav for the row alone, which tags a folder file;
# Remove deletes it, and on My Fav itself flags the list to reload as Now Playing's heart does.
m=QueueMachine(); assert m.run(2,steps=3)=='Added to Favourites' and m.names()==['A','B','C']
sel=lambda m:[c[:2] for c in m.calls if c[0].startswith('batch_')]
assert sel(m)==[('batch_init_selectrecord',20),('batch_set_selectitem',3),('batch_add_file',0xf001)]
assert [c[2:] for c in m.calls if c[0]=='batch_add_file']==[(0xf00a,m.get(syms['p_deque_showlist']))]; passed()
for cls,flag in ((0xf001,0),(0xf00a,1)):
    m=QueueMachine(cls=cls); m.favs.add('Row 0'); assert m.run(2)=='Removed from Favourites'
    assert [c[1] for c in m.calls if c[0]=='deleteMusicFromFav']==[m.row(0)] and m.u.mem_read(syms['g_delete_flag'],1)[0]==flag
    passed()
# Add to playlist opens the stock playlist page in its add mode (context: 1 << 16 | class) with the
# row selected, as the batch Add to playlist does; the page adds the row's songs itself.
for kw in ({},{'cls':0xf003,'page':'album_page'}):
    m=QueueMachine(**kw); assert m.run(3,steps=1) is None
    assert m.opened[-1]==('localmusic/playlist_page',0x10000|m.get(syms['g_class_type']))
    assert sel(m)==[('batch_init_selectrecord',20),('batch_set_selectitem',1)] and m.names()==['A','B','C']; passed()
# Shuffle plays the collection's songs from a random one with shuffle saved, as Shuffle Songs.
m=QueueMachine(cls=0xf003,page='album_page'); assert m.run(2) is None
assert m.opened[-1]==('playing_page',['T1','T2'],0,1,2) and [c[1:3] for c in m.calls if c[0]=='config_playmode']==[(2,1)]; passed()
m=QueueMachine(cls=0xf003,page='album_page'); m.found=[]; assert m.run(2)=='Queue unchanged' and m.opened[-1][0]!='playing_page'; passed()
# Go to album fills the album query as Now Playing's Album info and opens playerjumpinfo_page; Go to
# artist opens artistinfo_page with {class, record}. Closing either puts the browsing state back.
info=syms['g_local_classinfo_save']
for row,want in ((4,'playerjumpinfo_page'),(5,'localmusic/artistinfo_page')):
    m=QueueMachine(); m.u.mem_write(info,bytes(range(256))*3+bytes(144)); before=bytes(m.u.mem_read(info,912))
    page=m.top; assert m.run(row,steps=2) is None and m.opened[-1][0]==want and m.top!=page
    if row==4:
        assert m.get(info)==0xff10 and m.u.mem_read(info+9,1)==b'\0' and m.text(info+0xd)=='Album' and m.text(info+0x10d)=='Artist'
    else: assert m.opened[-1][1:]==(0xf001,m.row(2))
    f,ctx=m.handler(m.top,O['EVT_WINDOW_CLOSE']); m.call(address=f,args=(ctx,m.event,0,0),gap=0)
    assert bytes(m.u.mem_read(info,912))==before and m.names()==['A','B','C']; passed()
# An empty queue is filled by the stock loader without starting playback.
m=QueueMachine(queue=0); assert m.run(0) is None and m.names()==['Row 0'] and m.mcl('MCL_POS')==0
assert m.mcl('MCL_TYPE')==0xf001 and m.pool()==[0] and not m.playback(); passed()
# Gapless: a preload of pos+1 is closed exactly when the new tracks land there.
for row,pos,closed in ((0,0,True),(1,0,False),(1,2,True)):
    m=QueueMachine(pos=pos); m.word(O['MCL_PRELOAD'],1); m.word(O['MCL_FD'],9); m.run(row)
    assert (m.sent==[(9,b'{mcl-closegapless\\null}',23,0)] and m.mcl('MCL_PRELOAD')==-1) if closed else (not m.sent and m.mcl('MCL_PRELOAD')==1)
    passed()
# Shuffle: the patched mclNextSong call runs the stock pick, then plays the Play next track once.
demo=(B/'demo').read_bytes()
assert struct.unpack_from('<I',demo,fileoff(demo,SHUFFLE_CALL[0]))[0]==0x0c000000|symbols(B/'patch.elf')['ringnav_shuffle']>>2
m=QueueMachine(mode=2); m.run(0,steps=5); assert m.names()==['A','Row 5','B','C']
# Screen off, the UI loop idles SCREEN_OFF_SLEEP_MS before stock's own pacing; screen on, stock alone.
sleep_hook=HOOKS['main_loop_sleep_default'][0]
for light,want in ((1,[]),(0,[O['SCREEN_OFF_SLEEP_MS']])):
    s=Machine(); s.handlers[sleep_hook+12]='stock_sleep'; s.byte(syms['g_backlight_status'],light)
    assert s.call(address=sleep_hook,args=(0x1234,0,0,0),gap=0)==0
    assert [c[1] for c in s.calls if c[0]=='sleep_ms']==want and s.calls[-1][:2]==('stock_sleep',0x1234); passed()
    # The next pass with the backlight on repaints the whole screen once; staying on, nothing more.
    s.byte(syms['g_backlight_status'],1)
    for wake in (not light,False):
        s.call(address=sleep_hook,args=(0x1234,0,0,0),gap=0)
        assert [c[1] for c in s.calls if c[0]=='widget_invalidate_force']==([s.wm] if wake else [])
    passed()
for want in (1,3):
    m.call(address=syms['mclNextSong'],args=(0,0,0,0),gap=0)
    assert m.picks[-1]==1 and m.mcl('MCL_POS')==want and any(c[0]=='mclStartPlayer' for c in m.calls)
passed()
# Library sorts: both stock name comparators, on the patched trim calls, skip a leading article.
from build import SORT_TRIMS
from functools import cmp_to_key
for a in SORT_TRIMS: assert struct.unpack_from('<I',demo,fileoff(demo,a))[0]==0x0c000000|symbols(B/'patch.elf')['ringnav_sort_key']>>2
class SortMachine(Machine):
    def __init__(self):
        super().__init__(); self.handlers.pop(syms['toolsTrimLeft']); self.mock('strlen@GLIBC_2.0','snprintf@GLIBC_2.0')
        for n in ('__ctype_b_loc@GLIBC_2.3','__ctype_tolower_loc@GLIBC_2.3','strncmp@GLIBC_2.0','atoi@GLIBC_2.0',
                  'strcasecmp@GLIBC_2.0','strcmp@GLIBC_2.0','strncpy@GLIBC_2.0'): self.handlers[syms[n]]='s:'+n
        b=self.alloc(0x400); t=self.alloc(0x400)  # glibc's tables from -128: the digit class, ASCII lowercase
        for c in range(-128,256):
            self.u.mem_write(b+256+2*c,struct.pack('<H',0x800*(48<=c<58)))
            self.word(t+512+4*c,ord(chr(c).lower()) if 0<=c<128 else c)
        self.word(b,b+256); self.word(t,t+512); self.tables=(b,t)
    def hook(self,u,address,size,unused):
        name=self.handlers.get(address,'')
        if not name.startswith('s:'): return super().hook(u,address,size,unused)
        name=name[2:].split('@')[0]; a,b,c=[u.reg_read(r) for r in REGS[:3]]
        s=lambda x,n=None: bytes(u.mem_read(x,4096)).split(b'\0')[0][:n]
        if name.startswith('__ctype'): ret=self.tables[name=='__ctype_tolower_loc']
        elif name=='atoi': ret=int(re.match(rb'\d*',s(a))[0] or 0)
        elif name=='strncpy': u.mem_write(a,s(b,c).ljust(c,b'\0')); ret=a
        else: x,y=(s(a,c),s(b,c)) if name=='strncmp' else (s(a),s(b)); x,y=(x.lower(),y.lower()) if name=='strcasecmp' else (x,y); ret=(x>y)-(x<y)
        u.reg_write(UC_MIPS_REG_V0,ret&0xffffffff); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))
m=SortMachine()
def rec(name):
    r=m.alloc(0x60); m.word(r+0x10,m.string(name)); m.word(r+8,m.string(name+'.flac')); return r
def ordered(names,cmp):
    out=m.alloc(4)
    def less(x,y):  # the comparator stores x < y
        m.call(address=cmp,args=(x,y,out,0),gap=0,count=2_000_000); return m.get(out)
    rs={rec(n):n for n in names}
    return [rs[r] for r in sorted(rs,key=cmp_to_key(lambda x,y: -1 if less(x,y) else 1 if less(y,x) else 0))]
names=['Daft Punk','The Cure','Coldplay','Them Crooked Vultures','A Tribe Called Quest','Anthrax','The','an Orchestra','Toto']
for cmp in (0x5ba658,0x5b9d40):
    assert ordered(names,cmp)==['Anthrax','Coldplay','The Cure','Daft Punk','an Orchestra','The','Them Crooked Vultures','Toto','A Tribe Called Quest'],ordered(names,cmp)
    passed()
key=lambda t: (m.u.mem_write(0x1100000,t.encode()+b'\0'), m.call(address=symbols(B/'patch.elf')['ringnav_sort_key'],args=(0x1100000,0,0,0),gap=0), m.text(0x1100000))[2]
assert [key(t) for t in ('  The Cure','THE CURE','The ','The  Cure','A','An ','Ant','周杰伦','The 周杰伦')]==['Cure','CURE','The ','The  Cure','A','An ','Ant','周杰伦','周杰伦']; passed()
# iPod boots to Home: home_page_init's memory-play resume, run from its stock context build, starts
# the restored queue paused (mode 3) through player_start as playing_page_init would, and opens no
# page; a 0xff class starts nothing, as the page's init. Car mode (mode 2) opens Now Playing as stock.
if variant=='ipod':
    boot=symbols(B/'patch.elf')['ringnav_boot']
    assert struct.unpack_from('<I',demo,fileoff(demo,0x523de0))[0]==0x0c000000|boot>>2
    for car,cls,want in ((0,1,[(0x5550,4,1,3)]),(0,0xff,[]),(1,0xf003,[])):
        m=Machine(); m.word(syms['g_memory_info'],cls); m.byte(syms['g_carmode'],car)
        m.u.reg_write(UC_MIPS_REG_GP,0xa26cc0); m.u.reg_write(UC_MIPS_REG_SP,0x7000f000)
        m.u.reg_write(UC_MIPS_REG_S0,0x5550); m.u.reg_write(UC_MIPS_REG_S1,4)
        m.u.emu_start(0x523dac,0x523de8,count=1000)
        nav=[c for c in m.calls if c[0]=='navigator_to_with_context']
        assert m.started==want
        if car:
            assert len(nav)==1 and m.text(nav[0][1])=='playing_page' and nav[0][2]==0x7000f018
            assert [m.get(0x7000f018+4*i) for i in range(4)]==[0x5550,4,0xf003,2]
        else: assert not nav
        passed()
# Refusals leave the queue alone: a stream queue, or a row whose record changed under the menu.
m=QueueMachine(); m.word(O['MCL_TYPE'],2); assert m.run(0)=='Queue unchanged' and m.names()==['A','B','C']
m=QueueMachine(); m.press(1); m.hold(); m.release(); m.word(m.row(0)+O['REC_NAME'],m.string('other'))
m.pick(0); m.advance(0); assert m.toasts[-1][3]=='Queue unchanged' and m.names()==['A','B','C']; passed()
# Unsupported pages and states keep the stock long key: no menu, and the release is stock.
for setup,toggles in ((lambda m:m.byte(syms['g_lockscreen_pageflag'],1),1),(lambda m:m.byte(syms['g_backlight_status'],0),1),
        (lambda m:m.byte(syms['g_navbar_status'],1),1),
        (lambda m:m.word(m.surface+O['TABLE_ROWS'],7),1),(lambda m:m.word(m.row(0)+O['REC_TYPE'],4),1),
        (lambda m:setattr(m,'airplay',2),0),
        (lambda m:setattr(m,'top',m.node('window','home_page',[m.node()])),1)):
    m=QueueMachine(); setup(m); m.press(3)
    assert m.hold()==0 and len(m.stack)==1 and m.release()==toggles
    passed()
# Unknown rows (docs/internals.md#unknown-rows): stock's list (here the showlist as built, its size
# returned) keeps its trailing Unknown row (id -1) only when the query its press runs finds a song;
# the staging deque is left cleared. Other rows and classes stay stock.
class ClassMachine(QueueMachine):
    tramp=int(manifest['patch_symbols']['stock_localclass_trampoline'],16)
    def hook(self,u,address,size,unused):
        if address!=self.tramp: return super().hook(u,address,size,unused)
        self.calls.append(('stock_localclass',u.reg_read(REGS[0]),0,0))
        u.reg_write(UC_MIPS_REG_V0,len(self.items(self.get(syms['p_deque_showlist']))))
        u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))
def unknown(cls,found,last=-1,artist=0,rows=3):
    m=ClassMachine(page='localclass_page',cls=0xf001,rows=rows); m.found=[m.song('T1')]*found; m.query=None
    m.word(syms['artist_type'],artist); m.word(syms['g_artist_type'],1-artist)
    if rows: m.word(m.row(rows-1)+O['REC_ID'],last)
    n=m.call(address=HOOKS['load_localclass_list'][0],args=(cls,0,0,0),gap=0)
    shown=m.names(m.get(syms['p_deque_showlist']))
    # Artists follow the artist page's own switch (artist_type), which stock never copied to g_artist_type
    assert n==len(shown) and m.calls[0][:2]==('stock_localclass',cls) and m.get(syms['g_artist_type'])==artist
    return shown,m.query,m.items(m.get(syms['tools_pdeq_directory']))
for cls,artist,query in ((0xf003,0,'getMusicByAlbum'),(0xf004,0,'getMusicBySonger'),(0xf004,1,'getMusicByAlbumArtist'),
                         (0xf005,0,'getMusicByComposer'),(0xf006,0,'getMusicByGenre')):
    shown,q,staging=unknown(cls,0,artist=artist)
    assert shown==['Row 0','Row 1'] and q[:2]==(query,None) and not staging, (cls,artist)
    shown,q,staging=unknown(cls,1,artist=artist)
    assert shown==['Row 0','Row 1','Row 2'] and q[:2]==(query,None) and not staging, (cls,artist); passed()
for cls,last,rows in ((0xf004,1,3),(0xff01,-1,3),(0xf007,-1,3),(0xf004,-1,0)):
    shown,q,_=unknown(cls,0,last=last,rows=rows)
    assert len(shown)==rows and q is None, (cls,last,rows); passed()

# Coverflow (docs/internals.md): the Home card, the runtime coverflow_page over a stock slide_menu,
# the tracks query and handoff. The art thread itself runs on the host (test/coverflow.py).
from ipod import HOME_LIST_W, HOME_PAGE, HOME_ROW, HOME_ROWS, decode
cards=decode((B/'ui'/HOME_PAGE).read_bytes())[3][0]  # the carousel, or iPod's list_view
if variant=='ipod': cards=cards[3][0]  # its scroll_view of rows
cards=[c[2]['name'] for c in cards[3]]
assert len(cards)==7 and cards[2]=='btn_coverflow', cards
home_hook=HOOKS['home_page_init']
assert struct.unpack_from('<I',demo,fileoff(demo,home_hook[0]))[0]==0x08000000|symbols(B/'patch.elf')['coverflow_home']>>2
passed()
def asset_tree(m,path):
    """A built UI asset as mock widgets, the window's parent the window manager, with what the payload
    reads: type, name, visible, enable, geometry and, for a scroll view, its content height and the
    slidable flags as AWTK sets them. scroll_view_create (0x5f146c) leaves both off, the asset's
    xslidable/yslidable props set them (0x5f1d34), and a list_view's layout (0x5ea3a4) clears x and
    sets y only when the list holds a mobile scroll bar."""
    def build(n,parent,parent_kind='',siblings=()):
        kind,g,props,children=n
        a=m.node(kind,props.get('name',''),visible=int(props.get('visible')!='false'))
        m.nodes[a].update(enable=int(props.get('enable')!='false'),asset=props)
        for off,v in zip(('W_X','W_Y','W_W','W_H'),[0,0,375,290] if kind=='window' else g): m.word(a+O[off],v)
        m.word(a+O['W_PARENT'],parent)
        if kind=='scroll_view':
            m.byte(a+O['VIEW_HORIZONTAL'],props.get('xslidable')=='true' and parent_kind!='list_view')
            m.byte(a+O['VIEW_VERTICAL'],props.get('yslidable')=='true' or parent_kind=='list_view' and 'scroll_bar_m' in siblings)
            m.word(a+O['VIEW_CONTENT_H'],max((c[1][1]+c[1][3] for c in children),default=0))
        m.nodes[a]['children']=[build(c,a,kind,[c[0] for c in children]) for c in children]
        return a
    return build(decode((B/'ui'/path).read_bytes()),m.wm)
def named(m,w,name):
    if m.nodes[w]['name']==name: return w
    return next((f for c in m.nodes[w]['children'] if (f:=named(m,c,name))),0)
def click_target(m,w):
    em=m.alloc(4); it=m.alloc(0x28); m.word(w+O['W_EMITTER'],em); m.word(em,it); m.word(it+O['EMIT_TYPE'],O['EVT_CLICK'])
def home_list(m):
    """iPod Home as built (ui/home_page.bin) on top: its scroll view and the rows' tap images. Stock
    binds every image but Coverflow's, which the payload binds."""
    m.top=asset_tree(m,HOME_PAGE)
    imgs=[named(m,m.top,'img_'+r) for r in HOME_ROWS]
    for img in imgs[:2]+imgs[3:]: click_target(m,img)
    return named(m,m.top,'scroll_view_home'),imgs

# Return is a button too: after a touch, the page it goes back to shows its selection again.
m=Machine(); w,es=m.page_list(3); m.paint(w); m.touch(); m.paint(w,gap=0); assert not m.drawn()
assert m.call(O['KEY_RETURN'])==0; m.advance(0); m.paint(w,gap=0); assert m.drawn(); passed()

if variant=='ipod':
    # Page slides (navigation.c slides()): the page under a sliding top window draws the row it holds; no load, recall or scroll.
    HINT='htranslate'  # any non-empty anim_hint
    def under_slide(hint=HINT):
        m=Machine(); w,es=m.page_list(3); m.paint(w); assert m.call()==11 and m.selected(w)==1
        m.word(w+O['W_PARENT'],m.top); m.word(m.top+O['W_PARENT'],m.wm)
        w2,_=m.page_list(3,name='display_page'); m.nodes[m.top]['anim_hint']=hint
        return m,w,w2
    m,w,w2=under_slide(); m.paint(w,gap=0)
    assert m.drawn() and m.sel()[1]==48-m.get(w+O['SCROLL_Y']) and m.selected(w)==1 and not m.moved(); passed()
    m,w,w2=under_slide(''); m.paint(w,gap=0); assert not m.drawn(); passed()  # under a page that does not slide
    # Touch mode hides it there too, until a Return by button, which shows the page behind its row
    # while the closing page keeps hiding its own.
    m,w,w2=under_slide(); m.paint(w2); m.touch(); m.paint(w,gap=0); assert not m.drawn()
    assert m.call(O['KEY_RETURN'])==0; m.paint(w,gap=0); assert m.drawn()
    m.paint(w2,gap=0); assert not m.drawn(); passed()
    # A sliding page's first paint is its snapshot, so a recalled row is revealed at once, not by a glide.
    m,w,es=walk(10,5); w2,es2=m.page_list(10,extent=1000); m.nodes[m.top]['anim_hint']=HINT
    assert m.paint(w2)==0 and m.selected(w2)==5 and m.get(w2+O['SCROLL_Y'])==204
    assert [c[0] for c in m.moved()]==['scroll_view_set_offset']; passed()

    # Home is an ordinary list: the wheel walks the seven rows one by one, stops hard at both ends
    # (no carry-over, even after a pause), the bar spans the list's width and centre clicks the image.
    m=Machine(); view,imgs=home_list(m); click_target(m,imgs[2])
    m.paint(view)
    assert m.selected(view)==0
    for i in range(1,7): assert m.call()==11 and m.selected(view)==i and m.get(view+O['SCROLL_Y'])==0
    for gap in (1000,50,1000): assert m.call(gap=gap)==11 and m.selected(view)==6
    m.paint(view); assert m.sel()==(0,6*HOME_ROW,HOME_LIST_W,HOME_ROW)
    for i in range(5,-1,-1): assert m.call(O['KEY_PREV'])==11 and m.selected(view)==i
    for gap in (1000,50,1000): assert m.call(O['KEY_PREV'],gap=gap)==11 and m.selected(view)==0
    m.call(); m.call()
    assert not m.slides and m.confirm()==11 and m.dispatched()[0][1]==imgs[2]; passed()

    # Boot: if Home's first paint comes before the screen is usable, no row is chosen or drawn. The
    # status bar's next paint repaints the list, once, and Now Playing gets the bar.
    m=Machine(); view,imgs=home_list(m); bar=m.node('window','system_bar'); m.word(syms['system_bar'],bar); m.word(bar+O['W_PARENT'],m.wm)
    paint_bar=lambda: m.call(address=IPOD_HOOKS['widget_on_paint_background'][0],args=(bar,m.canvas,0,0))
    m.byte(syms['g_backlight_status'],0); m.paint(view); assert m.selected(view)==-1 and not m.drawn()
    paint_bar(); assert ('widget_invalidate_force',view) not in [c[:2] for c in m.calls]
    m.touch(); m.byte(syms['g_backlight_status'],1)
    paint_bar(); assert ('widget_invalidate_force',view) in [c[:2] for c in m.calls]
    m.paint(view); assert m.selected(view)==0 and m.sel()==(0,0,HOME_LIST_W,HOME_ROW)
    paint_bar(); assert ('widget_invalidate_force',view) not in [c[:2] for c in m.calls]; passed()

    # Chevrons: the stock list_into, where stock rows put img_into, on each visible row of a
    # drill window (contexts.inc), clipped to the surface; Home's only on its selection bar.
    # Stock draws its own on folder, category, album-list and Local Music rows, so those windows,
    # song lists and grids get none from here.
    def window(m,name,w):
        m.top=m.node('window',name,[w]); m.word(w+O['W_PARENT'],m.top); m.word(m.top+O['W_PARENT'],m.wm)
    def loaded(m): return [m.text(c[2]) for c in m.calls if c[0]=='widget_load_image']
    m=Machine(); view,imgs=home_list(m); click_target(m,imgs[2]); m.clip=(0,0,375,320)
    m.paint(view)
    half=O['CHEVRON_W']-25  # centre of the 50px image, as stock img_into
    bar=lambda: [(HOME_LIST_W-half,m.sel()[1]+HOME_ROW//2,(0,0,HOME_LIST_W,7*HOME_ROW))]
    assert m.sel()==(0,0,HOME_LIST_W,HOME_ROW) and m.icons==bar()
    assert loaded(m)==['list_into'] and m.clip==(0,0,375,320); passed()
    m.call(); m.paint(view); assert m.selected(view)==1 and m.sel()[1]>0 and m.icons==bar(); passed()  # follows the bar
    # Drawn in touch mode too, with Home's bar, which touch never hides; none while stock
    # multi-select hides its own.
    m.touch(); m.paint(view); assert m.icons==bar() and m.selected(view)==1; passed()
    m.byte(syms['g_navbar_status'],1); m.paint(view); assert not m.icons and not loaded(m); passed()
    # Playlists: every row follows the scroll, clipped to the viewport; the half-width
    # Import/Export tiles get none.
    m=Machine(); w,es=m.page_list(10,height=96,extent=480,name='playlist_page'); window(m,'playlist_page',w)
    tile=m.entry(w,0); m.word(tile+O['W_W'],100); m.nodes[w]['children'].insert(0,tile)
    for top in (0,24):
        m.word(w+O['SCROLL_Y'],top); m.paint(w)
        assert m.icons==[(240-half,48*i+24-top,(0,0,240,96)) for i in range(10)]; passed()
    # Leaf lists, grids and windows whose rows stock already marks draw none.
    for name in ('allmusic_page','albuminfo_page','artistinfo_page','album_page','folder_page',
                 'localclass_page','localmusic_page','sysset_page'):
        m=Machine(); w,_=m.page_list(5,extent=240,name=name); window(m,name,w); m.paint(w)
        assert not m.icons and not loaded(m), name; passed()

if variant=='ipod':
    # The status bar as built (ui/system_bar.bin), laid out by the stock row layouter
    # (children_layouter_default, 0x628838) with every icon shown: the icons, 16px high and drawn
    # centred in their 30px cells, and the title clear the glass's rounded top corners.
    from ipod import STATUS_BAR, corner_inset
    m=Machine(); m.mock('strtol@GLIBC_2.0','strstr@GLIBC_2.0'); m.mock('tk_calloc','tk_free',prefix='alloc:')
    bar=asset_tree(m,STATUS_BAR); m.word(bar+O['W_W'],375)
    views=[named(m,bar,n) for n in ('view_left','view_right')]
    for v in views:
        layout=m.call(address=syms['children_layouter_default_create'],args=(0,0,0,0),gap=0)
        for param in re.fullmatch(r'default\((.*)\)',m.nodes[v]['asset']['children_layout'])[1].split(','):
            assert m.call(address=syms['children_layouter_set_param_str'],args=(layout,*map(m.string,param.split('=')),0),gap=0)==0
        m.word(v+O['W_CHILDREN_LAYOUT'],layout)
    def laid_out():
        for v in views: native_row_layout(m,v)
        return [(signed(m.get(v+O['W_X']))+signed(m.get(c+O['W_X'])),m.get(c+O['W_W']),m.get(c+O['W_H'])) for v in views
                for c in m.nodes[v]['children'] if m.nodes[c]['visible']]
    cells=laid_out()
    inset=corner_inset((30-16)//2)
    assert all(h==30 and inset<=x and x+w<=375-inset for x,w,h in cells), (inset,cells)
    title=named(m,bar,'label_clock'); x,w=m.get(title+O['W_X']),m.get(title+O['W_W'])
    left,right=(cells[len(m.nodes[views[0]]['children'])-1],cells[len(m.nodes[views[0]]['children'])])
    assert left[0]+left[1]<=x and x+w<=right[0] and x+w/2==375/2, (left,right,x,w); passed()
    # Each Battery mode (bar_sync shows one of the three): the layout skips the hidden two, the
    # battery ends at the icons' margin, a percentage's text clears the corner there, and with
    # the plain Bluetooth glyph and Wi-Fi the group's ink stays CLOCK_GAP clear of the widest clock.
    from ipod import CLOCK_TEXT, CLOCK_GAP, STATUS_MARGIN, corner_x
    batt=[named(m,bar,n) for n in ('img_battery','label_battery','view_battery')]
    for mode,want in enumerate((10,O['BATT_PCT_W'],O['BATT_BODY_W']+O['BATT_NUB_W'])):
        for i,b in enumerate(batt): m.nodes[b]['visible']=int(i==mode)
        cells=laid_out(); bt=cells[len(m.nodes[views[0]]['children'])]
        assert cells[-1][0]+cells[-1][1]==375-STATUS_MARGIN and cells[-1][1]==want, (mode,cells)
        if mode==1: assert corner_x((30-O['BATT_PCT_PX'])//2,O['BATT_PCT_PX'])<=STATUS_MARGIN
        ink=bt[0]+bt[1]-O['BT_REACH']
        assert ink>=375/2+CLOCK_TEXT/2+CLOCK_GAP and 375-STATUS_MARGIN-ink<=O['BATT_ROOM'], (mode,cells)
    passed()

# Fast-scroll letter (iPod): once the wheel ramp moves more than one row per detent on a long list,
# the selected row's first character (a-z upper-cased, leading spaces skipped) is drawn centred over
# the list on a translucent dark rounded square, LETTER_MS after the last detent a timer repaints it
# away, and every canvas text/fill/clip state it touched is restored. Stock draws none.
def canvas_state(m):
    return (m.lcd_colors(),m.get(m.lcd+O['LCD_TEXT_COLOR']),m.get(m.canvas+O['CANVAS_ALIGN_V']),
            m.get(m.canvas+O['CANVAS_ALIGN_H']),m.clip)
def letter_machine(virtual,n=40):
    m=Machine(); m.word(m.canvas+O['CANVAS_ALIGN_V'],2)
    m.word(m.canvas+O['CANVAS_ALIGN_H'],3); m.word(m.lcd+O['LCD_TEXT_COLOR'],0x11223344)
    if virtual:
        w,rs,es=m.table_page(rebind=True); m.word(w+O['TABLE_ROWS'],n)
    else:
        w,es=m.page_list(n,extent=n*48,name='allmusic_page'); rs=es
    for e in es: m.nodes[e]['text']='row'
    m.paint(w,gap=0); return m,w,rs,es
def selected_entry(m,w,rs,es):
    return next(e for r,e in zip(rs,es) if (m.get(r+O['ROW_INDEX']) if r!=e else es.index(e))==m.selected(w))
box=(120-O['LETTER_BOX']//2,48-O['LETTER_BOX']//2,O['LETTER_BOX'],O['LETTER_BOX'])
for virtual in (False,True):
    m,w,rs,es=letter_machine(virtual); before=canvas_state(m)
    slow=1 if variant=='stock' else 3  # one-row ticks 100ms apart before the ramp's second row
    for _ in range(slow): assert m.call(gap=100)==11; m.paint(w,gap=0); assert not m.letters   # step 1
    assert m.call(gap=100)==11 and m.selected(w)==slow+2-(variant=='ipod'); m.paint(w,gap=0)     # step 2; iPod dropped its second tick
    if variant!='ipod':
        assert not m.letters and not m.timers and not any(c[0]=='canvas_set_font' for c in m.calls); passed(); continue
    assert m.letters==[dict(text='R',rect=box,color=0xffffffff,font=('default',O['LETTER_PX']),align=(1,1),
                            clip=(0,0,240,96))]
    fills=[r for r in m.rounded if r['kind']=='fill']
    assert fills==[dict(kind='fill',rect=box,bg=0,color=O['LETTER_ALPHA']<<24|O['FILL_RGB'],
                        radius=O['LETTER_RADIUS'],width=None,clip=(0,0,240,96))]
    assert canvas_state(m)==before; passed()
    # A library list shows the letter its row sorts under: a leading article is skipped (ringnav_sort_key).
    for text,glyph in (('zeta','Z'),('  apple','A'),('Émile','Émile'[0]),('東京','東'),('9 lives','9'),('The Cure','C'),
                       ('Them Crooked Vultures','T'),('a Tribe','T'),('An Émile','Émile'[0]),('Anthrax','A'),('The','T')):
        m.nodes[selected_entry(m,w,rs,es)]['text']=text; m.paint(w,gap=0)
        assert [l['text'] for l in m.letters]==[glyph],text; passed()
    m.nodes[selected_entry(m,w,rs,es)]['text']='   '; m.paint(w,gap=0)
    assert not m.letters and canvas_state(m)==before; passed()
    m.nodes[selected_entry(m,w,rs,es)]['text']='row'
    # A further fast detent re-arms; the letter clears LETTER_MS after the last one.
    assert m.call(gap=100)==11; m.advance(O['LETTER_MS']-1); m.paint(w,gap=0); assert m.letters
    m.advance(1); assert any(c[:2]==('widget_invalidate_force',w) for c in m.calls)
    m.paint(w,gap=0); assert not m.letters and canvas_state(m)==before; passed()
if variant=='ipod':
    # Never at one row per detent, nor on a list of at most SHORT_LIST_MAX (16) rows.
    for n,gap in ((40,141),(16,100)):
        m,w,rs,es=letter_machine(False,n)
        for _ in range(6): m.call(gap=gap)
        m.paint(w,gap=0); assert not m.letters and not m.timers; passed()
    # Touch drops the spin, and with it the letter.
    m,w,rs,es=letter_machine(False)
    for _ in range(5): m.call(gap=100)
    m.paint(w,gap=0); assert m.letters
    m.touch(); m.paint(w,gap=0); assert not m.letters; passed()
    # Elsewhere (a folder sorts by its file name) the first character stays.
    m=Machine(); w,es=m.page_list(40,extent=40*48,name='folder_page')
    for e in es: m.nodes[e]['text']='The Cure'
    for _ in range(5): m.call(gap=100)
    m.paint(w,gap=0); assert [l['text'] for l in m.letters]==['T']; passed()

WRITERS={'scanAllMusicFile':'stock_scan_all','scanSpecFolder':'stock_scan_folder','deleteMusicFromMusicDb':'stock_delete_song'}
class CoverflowMachine(QueueMachine):
    FREE=0x1000010  # stock free's GOT slot is 0 until lazy binding; give it a stub
    def __init__(self,albums=3,cached=True,**queue):
        super().__init__(rows=4,**queue)
        self.cached=cached; self.missing=False; self.joins=[]; self.threads=[]; self.plays=[]; self.homes=0; self.freed=0
        for n in ('window_create','widget_factory_create_widget','image_base_set_image','getAllAlbum','list_view_create',
                  'scroll_view_create','navigator_back_to_home','navigator_to_with_context','access@GLIBC_2.0',
                  'calloc@GLIBC_2.0','strdup@GLIBC_2.0','mkdir@GLIBC_2.0','statfs@GLIBC_2.0','pthread_create@GLIBC_2.2',
                  'pthread_join@GLIBC_2.0','fopen@GLIBC_2.2','qsort@GLIBC_2.0','toolsQueryDbTable'): self.handlers[syms[n]]='c:'+n
        self.word(0xa2638c,self.FREE); self.handlers[self.FREE]='c:free'
        self.handlers[home_hook[0]+12]='stock_home'
        for n,stub in WRITERS.items(): self.handlers[HOOKS[n][0]+12]=stub  # the writers' stock bodies
        self.albums=[self.song('T0') for _ in range(albums)]
        for i,r in enumerate(self.albums): self.word(r+O['REC_ALBUM'],self.string(f'Album {i}'))
        self.found=[self.song('T1'),self.song('T2')]
    def hook(self,u,address,size,unused):
        name=self.handlers.get(address,'')
        if not name.startswith('c:'): return super().hook(u,address,size,unused)
        assert u.reg_read(UC_MIPS_REG_T9)==address, name
        name=name[2:].split('@')[0]; a,b,c,d=[u.reg_read(r) for r in REGS]; sp=u.reg_read(UC_MIPS_REG_SP); ret=0
        self.calls.append((name,a,b,c))
        if name in ('window_create','widget_factory_create_widget','list_view_create','scroll_view_create'):
            kind,parent,h={'window_create':('window',0,0),'widget_factory_create_widget':(self.text(b),c,self.get(sp+24))}.get(
                name,(name.removesuffix('_create'),a,self.get(sp+16)))
            ret=self.node(kind)
            if kind=='window': self.top=ret; self.stack.append(ret)
            else: self.nodes[parent]['children'].append(ret); self.word(ret+O['W_PARENT'],parent); self.word(ret+O['W_H'],h)
            if name=='widget_factory_create_widget':  # x in a3; y, w on the stack
                for off,v in (('W_X',d),('W_Y',self.get(sp+16)),('W_W',self.get(sp+20))): self.word(ret+O[off],v)
            if kind=='slide_menu': self.word(ret+O['SLIDE_INDEX'],0); self.word(ret+0x5c,self.alloc())
        elif name=='image_base_set_image': self.nodes[a]['image']=self.text(b)
        elif name=='getAllAlbum':
            self.deqs[self.get(syms['tools_pdeq_directory'])][1]=[self.copy('stSongInfo',e) for e in self.albums]; ret=len(self.albums)
        elif name=='navigator_back_to_home': self.homes+=1
        elif name=='navigator_to_with_context':
            if self.text(a)=='playing_page':
                self.play_names=self.names(self.get(b))
                self.plays.append((self.text(a),*[signed(self.get(b+4*i)) for i in range(4)]))
            elif self.text(a)=='dialog/msginfo_dialog': self.toasts.append((self.text(a),self.get(b),self.get(b+4),self.text(b+8)))
            else: self.opened.append((self.text(a),self.get(b),self.get(b+4))); self.top=self.node('window',self.text(a)); self.stack.append(self.top)
        elif name=='access': path=self.text(a); ret=0 if (self.cached if '/mnt/mmc/.coverflow/' in path else not self.missing) else -1
        elif name=='fopen': ret=0  # no saved album: remember() keeps the first
        elif name=='toolsQueryDbTable': self.deqs[self.get(syms['tools_pdeq_directory'])][1]=[]
        elif name=='qsort':  # sorted by the payload's comparator, run nested on a stack below this one
            regs=[UC_MIPS_REG_PC,*range(UC_MIPS_REG_0,UC_MIPS_REG_31+1)]; saved=[u.reg_read(r) for r in regs]; pair=self.alloc(2*c)
            def compare(x,y):
                self.u.mem_write(pair,x); self.u.mem_write(pair+c,y)
                for r,v in ((UC_MIPS_REG_SP,sp-0x400),(UC_MIPS_REG_RA,0x1000000),(UC_MIPS_REG_T9,d),(REGS[0],pair),(REGS[1],pair+c)): u.reg_write(r,v)
                u.emu_start(d,0x1000000,count=self.budget); return signed(u.reg_read(UC_MIPS_REG_V0))
            for i,v in enumerate(sorted([bytes(u.mem_read(a+c*i,c)) for i in range(b)],key=cmp_to_key(compare))): u.mem_write(a+c*i,v)
            for r,v in zip(regs,saved): u.reg_write(r,v)
        elif name=='calloc': ret=self.alloc(a*b+4)
        elif name=='strdup': ret=self.string(self.text(a))
        elif name=='free': self.freed+=a!=0
        elif name=='statfs': self.word(b+4,4096); self.word(b+28,1<<20)
        elif name=='pthread_create': self.word(a,77); self.threads.append((c,d))
        elif name=='pthread_join': self.joins.append(a)
        for r in [UC_MIPS_REG_V1,*REGS,UC_MIPS_REG_T8,UC_MIPS_REG_T9]: u.reg_write(r,0xdeadbeef)
        u.reg_write(UC_MIPS_REG_V0,ret&0xffffffff); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))
    def open(self):
        """Home with the card at index 2: centre confirms its image, whose click opens Coverflow."""
        if variant=='ipod':
            view,self.imgs=home_list(self); self.img=self.imgs[2]; self.home=self.top; self.stack=[self.top]
            self.art=named(self,self.top,'img_homeart'); self.list=named(self,self.top,'list_view_home')
            assert self.call(address=home_hook[0],args=(self.top,0,0,0),gap=0)==0
            self.paint(view); self.nodes[view]['_ringnav_index']=2
        else:
            slide=self.node('slide_menu'); self.word(slide+O['SLIDE_INDEX'],2); self.word(slide+0x5c,self.alloc())
            self.img=self.node('image','img_coverflow'); card=self.node('button',children=[self.img])
            self.nodes[slide]['children']=[self.entry(slide),self.entry(slide),card,*[self.entry(slide) for _ in range(4)]]
            self.home=self.top=self.node('window','home_page',[slide]); self.stack=[self.top]
            assert self.call(address=home_hook[0],args=(self.top,0,0,0),gap=0)==0
        self.clicks=[]; assert self.confirm()==11 and self.clicks==[self.img]
        f,ctx=self.handler(self.img,O['EVT_CLICK']); assert self.call(address=f,args=(ctx,self.event,0,0),gap=0)==0
        self.page=self.top; self.slide=self.find('slide_menu')
        return self.page
    def find(self,kind,w=None):
        w=w or self.page
        if self.nodes[w]['type']==kind: return w
        return next((f for c in self.nodes[w]['children'] if (f:=self.find(kind,c))),0)
    def texts(self,kind='hscroll_label'): return [self.nodes[w].get('text') for w in self.nodes if self.nodes[w]['type']==kind and self.alive(w)]
    def tracks(self,album=0):
        f,ctx=self.handler(self.nodes[self.slide]['children'][album],O['EVT_CLICK'])
        self.call(address=f,args=(ctx,self.event,0,0),gap=0); self.advance(0)
        view=self.find('scroll_view'); self.paint(view)
        return view
    def rescan(self,writer='scanAllMusicFile'):
        """A songtable writer runs: its hook, then its (mocked) stock body."""
        assert self.call(address=HOOKS[writer][0],args=(1,2,0,0),gap=0)==0 and self.calls[0][:3]==(WRITERS[writer],1,2)
    def queried(self): return sum(c[0]=='getAllAlbum' for c in self.calls)
    def close(self):
        f,ctx=self.handler(self.page,O['EVT_DESTROY'])
        self.call(address=f,args=(ctx,self.event,0,0),gap=0)
    def alive(self,w):
        """Still attached below the page (the destroy_children mock only unlinks)."""
        while w!=self.page:
            parent=self.get(w+O['W_PARENT'])
            if parent not in self.nodes or w not in self.nodes[parent]['children']: return False
            w=parent
        return True
    def key(self,k=O['KEY_RETURN']):
        f,ctx=self.handler(self.page,O['EVT_KEY_UP']); ev=self.alloc(0x40); self.word(ev,O['EVT_KEY_UP']); self.word(ev+O['EVENT_KEY'],k)
        ret=self.call(address=f,args=(ctx,ev,0,0),gap=0); self.advance(0); return ret

if variant=='ipod':
    # The art follows the playing track through the paint hook, when Home or the status bar paints:
    # the Coverflow thumbnail of the queue's current album until the player has parsed this track
    # (g_lastcover_url holds its path), then the player's embedded cover. The list keeps its width.
    m=CoverflowMachine(); m.open(); m.top=m.home
    bar=m.node('window','system_bar'); m.word(syms['system_bar'],bar)
    for w in (bar,m.home): m.word(w+O['W_PARENT'],m.wm)
    def art_after(w):
        m.call(address=IPOD_HOOKS['widget_on_paint_background'][0],args=(w,m.canvas,0,0))
        return m.nodes[m.art].get('image')
    m.byte(syms['g_playcover_type'],1)
    assert art_after(m.home).startswith('file:///mnt/mmc/.coverflow/') and m.get(m.list+O['W_W'])==HOME_LIST_W
    m.u.mem_write(syms['g_lastcover_url'],b'/p/A\0'); assert art_after(bar)=='file:///tmp/coverpic.jpg'
    m.nodes[m.art]['image']='unchanged'; assert art_after(m.home)=='unchanged'  # same track and cover
    m.byte(syms['g_playcover_type'],3); assert art_after(m.home).startswith('file:///mnt/mmc/.coverflow/'); passed()

    # The art fills the right panel below the status bar: sized to the cover's proportions, just
    # covering the panel and centred on it (native fill then draws it whole), and clipped to the
    # panel from the background hook to the border hook, so it crops evenly and never stretches.
    PW=375-HOME_LIST_W
    assert m.nodes[m.art].get('sensitive')==0  # a fitted cover reaching under the list never takes its taps
    def geometry(): return [signed(m.get(m.art+O[k])) for k in ('W_X','W_Y','W_W','W_H')]
    def cover(w,h):
        m.image_size=(w,h)
        m.u.mem_write(syms['g_lastcover_url'],b'/p/other\0')  # a new key: the art reloads
        art_after(m.home); m.u.mem_write(syms['g_lastcover_url'],b'/p/A\0'); art_after(m.home)
        return geometry()
    # C division truncates: the extra pixel of an odd overflow is cropped on the right or bottom.
    for (w,h),want in (((300,300),[HOME_LIST_W+int((PW-290)/2),0,290,290]),     # square: crop the sides
                       ((500,1000),[HOME_LIST_W,0,PW,290]),                  # the panel's own 1:2
                       ((600,400),[HOME_LIST_W+int((PW-435)/2),0,435,290]),      # landscape
                       ((100,400),[HOME_LIST_W,int((290-580)/2),PW,580])):       # taller than the panel
        assert cover(w,h)==want,((w,h),geometry(),want)
        x,y,gw,gh=want; assert gw*h==w*gh or abs(gw/gh-w/h)<0.01  # proportional, never stretched
    passed()
    # The paint hooks clip it to the panel on screen (window at y 30) and restore the clip after.
    m.clip=(0,0,375,320); m.word(m.canvas+O['CANVAS_X'],HOME_LIST_W); m.word(m.canvas+O['CANVAS_Y'],30-145)
    m.call(address=IPOD_HOOKS['widget_on_paint_background'][0],args=(m.art,m.canvas,0,0))
    assert m.clip==(HOME_LIST_W,30,PW,290),m.clip
    m.call(address=HOOKS['widget_on_paint_border'][0],args=(m.art,m.canvas,0,0))
    assert m.clip==(0,0,375,320); passed()
    # Other widgets keep the clip; the placeholder (no size) fills the panel.
    m.call(address=IPOD_HOOKS['widget_on_paint_background'][0],args=(m.list,m.canvas,0,0)); assert m.clip==(0,0,375,320)
    m.image_size=None
    m.u.mem_write(syms['g_lastcover_url'],b'/p/none\0'); m.byte(syms['g_playcover_type'],3)
    art_after(m.home); assert m.nodes[m.art]['image']=='default_album_big' and geometry()==[HOME_LIST_W,0,PW,290]; passed()

    # Now Playing: stock init runs first, then "n of m", the album and "-remaining" (slider max less
    # value, in seconds) fill in; later paints rewrite a label only when its source changed.
    playing=IPOD_HOOKS['playing_page_init'][0]
    assert struct.unpack_from('<I',demo,fileoff(demo,playing))[0]==0x08000000|symbols(B/'patch.elf')['ringnav_playing']>>2
    m=QueueMachine(queue=3,pos=1); m.handlers[playing+12]='stock_playing'
    for i,r in enumerate(m.items(m.get(syms['mcl_pdeqplaylist']))): m.word(r+O['REC_ALBUM'],m.string(f'Album {i}'))
    bar=m.node('window','system_bar'); m.word(syms['system_bar'],bar)
    pos,album,remain=(m.node('label',n) for n in ('label_ipod_pos','label_ipod_album','label_ipod_remain'))
    slider=m.node('slider','slider_play',max=225,value=100)
    win=m.node('window','playing_page',[m.node('view','view_buttons',[pos]),album,slider,remain])
    for w in (bar,win): m.word(w+O['W_PARENT'],m.wm)
    m.top=win
    def shown(): return [m.nodes[w].get('text') for w in (pos,album,remain)]
    def written(): return [m.nodes[c[1]]['name'] for c in m.calls if c[0]=='widget_set_text_utf8']
    def repaint(w=win): m.call(address=IPOD_HOOKS['widget_on_paint_background'][0],args=(w,m.canvas,0,0)); return written()
    assert m.call(address=playing,args=(win,7,0,0),gap=0)==0 and m.calls[0][:3]==('stock_playing',win,7)
    assert shown()==['2 of 3','Album 1','-02:05']; passed()
    assert repaint()==[] and repaint(bar)==[]; passed()
    m.nodes[slider]['value']=101; assert repaint()==['label_ipod_remain'] and shown()[2]=='-02:04'; passed()
    m.word(O['MCL_POS'],2); assert repaint(bar)==['label_ipod_pos','label_ipod_album'] and shown()[:2]==['3 of 3','Album 2']; passed()
    # A rebuilt queue can reuse the same string address with new text; the text itself is hashed.
    m.u.mem_write(m.get(m.items(m.get(syms['mcl_pdeqplaylist']))[2]+O['REC_ALBUM']),b'Other\0')
    assert 'label_ipod_album' in repaint() and shown()[1]=='Other'; passed()
    # Issue #7: once the player has parsed the playing file (g_play_id3_info starts with its path)
    # the label shows the album it parsed, which a next folder's untagged records lack.
    id3=syms['g_play_id3_info']; m.u.mem_write(id3+O['ID3_ALBUM'],b'Parsed\0')
    m.u.mem_write(id3,b'/p/other\0'); assert repaint()==[] and shown()[1]=='Other'
    m.u.mem_write(id3,b'/p/C\0'); assert repaint()==['label_ipod_pos','label_ipod_album'] and shown()[1]=='Parsed'
    m.u.mem_write(id3,b'\0'); assert repaint()==['label_ipod_pos','label_ipod_album'] and shown()[1]=='Other'; passed()
    for mx,v,want in ((3725,0,'-01:02:05'),(3600,0,'-01:00:00'),(3599,0,'-59:59'),(90,90,'-00:00'),(90,95,'-00:00')):
        m.nodes[slider].update(max=mx,value=v); repaint(); assert shown()[2]==want,(mx,v)
    passed()
    queue=m.deqs[m.get(syms['mcl_pdeqplaylist'])][1]
    del queue[1:]; m.word(O['MCL_POS'],0); repaint(); assert shown()[:2]==['1 of 1','Album 0']
    queue.clear(); repaint(); assert shown()[:2]==['','']; passed()
    # Another top window, or the page once destroyed, leaves the labels alone.
    queue.append(m.copy('stSongInfo',m.song('X'))); m.top=m.node('window','home_page'); m.word(m.top+O['W_PARENT'],m.wm)
    assert repaint(m.top)==[] and repaint(bar)==[]; m.top=win
    f,ctx=m.handler(win,O['EVT_DESTROY']); m.call(address=f,args=(ctx,m.event,0,0),gap=0)
    assert repaint()==[] and shown()[0]==''; passed()

    # Volume: with the stock volume dialog directly over Now Playing, the dialog's own paint draws
    # the band from the progress bar to the times: black, the track, a white fill to the volume and
    # "Volume N". Its slider and label hide, the slider moved onto the band so stock's invalidation
    # repaints it. Over any other window it stays stock.
    m=QueueMachine(queue=3,pos=1); m.handlers[playing+12]='stock_playing'
    slider,elapsed,remain=m.node('slider','slider_play',max=225,value=100,bar_size=8),m.node('label','label_playtime'),m.node('label','label_ipod_remain')
    win=m.node('window','playing_page',[slider,elapsed,remain]); m.word(win+O['W_PARENT'],m.wm); m.top=win
    def put(w,*g):
        for k,v in zip(('W_X','W_Y','W_W','W_H'),g): m.word(w+O[k],v)
    put(win,0,30,375,290); put(slider,24,240,327,30); put(elapsed,16,265,80,16)
    m.word(syms['system_bar'],m.node('window','system_bar'))
    assert m.call(address=playing,args=(win,7,0,0),gap=0)==0
    sv,lv=m.node('slider','slider_vol',max=100,value=40),m.node('label','label_vol')
    dlg=m.node('dialog','volume_dialog',[sv,lv]); m.word(dlg+O['W_PARENT'],m.wm); put(dlg,0,0,375,320)
    def paint(w): m.call(address=IPOD_HOOKS['widget_on_paint_background'][0],args=(w,m.canvas,0,0))
    def did(n): return [c[1:] for c in m.calls if c[0]==n]
    band=(0,270,375,30+265+16-270); bar=(24,281,327,8)
    m.nodes[m.wm]={'children':[win,dlg]}; m.top=dlg; paint(dlg)
    assert [m.nodes[w]['visible'] for w in (sv,lv,slider,elapsed)]==[0,0,1,1]
    assert [signed(m.get(sv+O[k])) for k in ('W_X','W_Y','W_W','W_H')]==list(band)
    fills=[b[:5] for b in m.bands]
    assert fills==[(*band,color_t(0))],fills  # then the capsule, as the progress bar
    assert [(r['rect'],r['radius'],r['color']) for r in m.rounded]==[(bar,4,color_t(O['TRACK_COLOR'])),((24,281,327*40//100,8),4,color_t(0xffffff))],m.rounded
    assert [(t['text'],t['rect'],t['font'][1]) for t in m.letters]==[('Volume 40',(0,295,375,16),O['NP_TIMES_PX'])]; passed()
    assert len(m.timers)==1; poll=next(iter(m.timers))
    m.advance(O['VOL_POLL_MS']); assert not did('widget_invalidate_force') and poll in m.timers  # unchanged: nothing
    m.nodes[sv]['value']=41; m.advance(O['VOL_POLL_MS'])  # changed: the whole dialog repaints
    assert [c[0] for c in did('widget_invalidate_force')]==[dlg]; passed()
    m.nodes[sv]['value']=100; paint(dlg)
    assert m.rounded[-1]['rect'][2]==327 and m.letters[0]['text']=='Volume 100' and len(m.timers)==1; passed()

    m.nodes[m.wm]['children']=[win]; m.top=win; m.advance(O['VOL_POLL_MS']); assert not m.timers; passed()  # closed: it stops
    # Over any other window (Quick Settings included) a rounded panel in the fast-scroll letter's
    # style holds a pill bar on a grey track and the number, clear of the glass corners.
    other=m.node('window','home_page'); m.word(other+O['W_PARENT'],m.wm); sv2,lv2=m.node('slider','slider_vol',max=100,value=7),m.node('label','label_vol')
    dlg2=m.node('dialog','volume_dialog',[sv2,lv2]); m.word(dlg2+O['W_PARENT'],m.wm); put(dlg2,0,0,375,320)
    m.nodes[m.wm]['children']=[win,other,dlg2]; m.top=dlg2; paint(dlg2)
    X,Y,H,P,T=(O['VOL_PANEL_'+k] for k in ('X','Y','H','PAD','TEXT')); panel=(X,Y,375-2*X,H); bw=panel[2]-2*P-T
    assert [m.nodes[w]['visible'] for w in (sv2,lv2)]==[0,0] and [signed(m.get(sv2+O[k])) for k in ('W_X','W_Y','W_W','W_H')]==list(panel)
    BH=O['VOL_PANEL_BAR']; bar=(X+P,Y+(H-BH)//2,bw,BH)
    assert [(r['rect'],r['radius'],r['color']) for r in m.rounded]==[(panel,O['LETTER_RADIUS'],(O['LETTER_ALPHA']<<24)|O['FILL_RGB']),
        (bar,BH//2,color_t(O['VOL_PANEL_TRACK'])),((*bar[:2],bw*7//100,BH),BH//2,color_t(0xffffff))],m.rounded
    assert not m.bands and [(t['text'],t['rect'],t['font'][1]) for t in m.letters]==[('7',(X+P+bw,Y,T,H),O['VOL_PANEL_PX'])]; passed()
    m.nodes[sv2]['value']=1; paint(dlg2)  # a sliver is still a round dot
    assert m.rounded[2]['rect']==(*bar[:2],BH,BH),m.rounded; passed()
    R=O['LETTER_RADIUS']; assert math.hypot(80-(X+R),Y+H-R-240)+R<=80  # its rounded corner clears the glass's

    # The art's corners: after the image (the border hook), each of the NP_ART_RADIUS rows of a
    # corner is black outside the arc, then one edge pixel at the alpha it leaves uncovered.
    m=QueueMachine(queue=3,pos=1); m.handlers[playing+12]='stock_playing'
    cover=m.node('image','img_cover'); win=m.node('window','playing_page',[cover]); m.word(win+O['W_PARENT'],m.wm); m.top=win
    put(cover,16,10,166,166); m.call(address=playing,args=(win,7,0,0),gap=0)
    m.word(m.lcd+O['LCD_FILL_COLOR'],0x12345678)
    m.bands.clear(); m.call(address=HOOKS['widget_on_paint_border'][0],args=(cover,m.canvas,0,0))
    R=O['NP_ART_RADIUS']; want=[]
    for i in range(R):
        out=16*R-math.isqrt((4*R*R-(2*(R-i)-1)**2)*64); n=out>>4
        for c in range(4):
            y=166-1-i if c&1 else i; left=not c&2
            want+=[(0 if left else 166-n,y,n,1,color_t(0)),(n if left else 166-n-1,y,1,1,(out&15)*17<<24)]
    assert [b[:5] for b in m.bands]==want and want[0][2]==8 and want[-2][2]==0,want[:2]
    assert m.get(m.lcd+O['LCD_FILL_COLOR'])==0x12345678; passed()  # restored
    m.bands.clear(); m.call(address=HOOKS['widget_on_paint_border'][0],args=(win,m.canvas,0,0)); assert not m.bands; passed()

    # Now Playing: one centre press cycles the four play modes after the double-click window. A
    # second press cancels the mode change and turns the screen off through the stock path.
    FG='style:normal:fg_color'
    def np_page(value=100,mx=225):
        m=QueueMachine(queue=3,pos=1); m.handlers[playing+12]='stock_playing'
        m.slider=m.node('slider','slider_play',max=mx,value=value)
        m.elapsed,m.remain=m.node('label','label_playtime'),m.node('label','label_ipod_remain')
        m.win=m.top=m.node('window','playing_page',[m.slider,m.elapsed,m.remain])
        m.word(m.win+O['W_PARENT'],m.wm)
        assert m.call(address=playing,args=(m.win,7,0,0),gap=0)==0
        return m
    def did(m,name): return [c[1] for c in m.calls if c[0]==name]
    def keep(m):
        m.starts=getattr(m,'starts',[])+did(m,'playing_timer_start')
    def step(m,*a,**k):  # one input or timer step, keeping every timer restart it makes
        r=(m.advance if k.pop('wait',False) else m.call)(*a,**k); keep(m); return r
    for mode in range(4):
        m=np_page(); m.word(O['MCL_MODE'],mode)
        assert m.call(O['KEY_CENTER'])==11
        m.advance(DC-1); assert not [c for c in m.calls if c[0]=='config_playmode']
        m.advance(1)
        assert [c[1:3] for c in m.calls if c[0]=='config_playmode']==[(((mode+1)%4),1)]
        assert not m.screens; passed()
    m=np_page(); m.word(O['MCL_MODE'],2)
    assert m.call(O['KEY_CENTER'])==11 and m.call(O['KEY_CENTER'],gap=100,clear=False)==11
    m.advance(DC)
    assert not [c for c in m.calls if c[0]=='config_playmode'] and m.screens==[0]
    assert not m.u.mem_read(syms['g_backlight_status'],1)[0]; passed()
    # The wheel's transparent volume dialog still belongs to Now Playing's Centre gesture.
    for double in (False,True):
        m=np_page(); m.nodes[m.wm]={'children':[m.win]}
        volume=m.node('dialog','volume_dialog'); m.nodes[m.wm]['children'].append(volume); m.top=volume
        assert Machine.release(m)==11
        if double: assert Machine.release(m,100)==11
        m.advance(DC)
        assert m.screens==([0] if double else [])
        assert [c[1:3] for c in m.calls if c[0]=='config_playmode']==([] if double else [(1,1)]); passed()
    for field in ('animating','pressed'):
        m=np_page(); setattr(m,field,1)
        assert Machine.release(m)==11
        m.advance(DC); assert not m.screens and m.mcl('MCL_MODE')==0; passed()
    # The lyrics page (slide value 1) with lyrics: the wheel scrolls scroll_lrc LYRIC_STEP a tick within
    # its content ahead of the volume, with stock's timer (which re-pins the current line)
    # stopped until SCRUB_MS after the last tick. Another page or no lyrics leaves the wheel on volume.
    def lyric_page(page=1,size=9,lines=1):
        m=np_page(); m.lyric_size=size
        m.lrc=m.node('scroll_view','scroll_lrc',[m.node('label') for _ in range(lines)])
        m.word(m.lrc+O['W_H'],178); m.word(m.lrc+O['VIEW_CONTENT_H'],250)
        m.slide=m.node('slide_view','slide_view',[m.lrc],value=page)
        m.nodes[m.win]['children'].append(m.slide)
        assert m.call(address=playing,args=(m.win,7,0,0),gap=0)==0; m.calls=[]
        return m
    for kw in ({'page':0},{'size':0},{'lines':0}):
        m=lyric_page(**kw); assert m.call()==0 and not did(m,'playing_timer_clear'),kw; passed()
    m=lyric_page()
    assert step(m)==11 and m.get(m.lrc+O['SCROLL_Y'])==O['LYRIC_STEP'] and did(m,'playing_timer_clear')==[m.win]
    for _ in range(5): assert step(m,gap=200)==11
    assert m.get(m.lrc+O['SCROLL_Y'])==72 and not did(m,'playing_timer_clear')  # 250 - 178
    for _ in range(5): assert step(m,O['KEY_PREV'],gap=200)==11
    assert m.get(m.lrc+O['SCROLL_Y'])==0 and not m.starts
    step(m,O['SCRUB_MS']-1,wait=True); assert not m.starts
    step(m,1,wait=True)
    assert m.starts==[m.win] and not m.timers and not did(m,'player_seek_time'); passed()
    # Touch ends it at once; the page's destruction keeps stock's timer off.
    m=lyric_page(); step(m); step(m,address=HOOKS['on_wm_tsdown_before_fun'][0],event_type=O['EVT_POINTER_DOWN'],gap=50)
    assert m.starts==[m.win] and not m.timers; passed()
    m=lyric_page(); step(m); f,ctx=m.handler(m.win,O['EVT_DESTROY']); step(m,address=f,args=(ctx,m.event,0,0),gap=0)
    assert not m.starts and not m.timers; passed()

# The Home card is index 2 of seven; its click opens coverflow_page with every album as a cover
# plus the Sort and Refresh cards, the wheel steps the stock slide_menu and centre confirms the cover.
m=CoverflowMachine(); page=m.open()
assert m.nodes[page]['name']=='coverflow_page' and m.slide and not m.threads
covers=m.nodes[m.slide]['children']; assert len(covers)==5
assert all(m.nodes[c]['image'].startswith('file:///mnt/mmc/.coverflow/') for c in covers[:3])
assert m.nodes[covers[-2]]['image']==m.nodes[covers[-1]]['image']=='default_album_big'
assert 'Album 0' in m.texts(); passed()
m.press(3); assert m.hold()==11 and m.nodes[m.title]['text']=='Album 0'
f,ctx=m.handler(m.back,O['EVT_CLICK']); m.call(O['KEY_RETURN'],address=f,args=(ctx,m.event,0,0),gap=0)
assert m.top==page and m.release()==0; passed()
m.press(3,O['KEY_PLAY']); assert m.hold(O['KEY_PLAY'])==11
assert m.opened[-1]==('playing_page',0,0,255,2) and m.top==page
assert m.release(O['KEY_PLAY'])==0 and not m.playback(); passed()
# Covers step like Home: fast ticks retarget one animator, never stock next/previous, whose
# scroll_to orphans the running animator.
assert m.call(gap=0)==11; a,_,to,dur=slide(m,m.slide); assert to<0 and dur==200
assert m.call(gap=20)==11 and slide(m,m.slide)[0]==a and slide(m,m.slide)[2:]==(2*to,120)
m.advance(300); assert m.get(m.slide+O['SLIDE_INDEX'])==2 and not m.slides and not m.get(m.slide+O['SLIDE_OFFSET'])
assert m.call(O['KEY_PREV'],gap=20)==11 and slide(m,m.slide)[2]==-to; m.advance(300)
assert m.get(m.slide+O['SLIDE_INDEX'])==1 and not m.slides
assert not any(c[0].startswith('slide_menu_scroll_to_') for c in m.calls)
m.clicks=[]; m.word(m.slide+O['SLIDE_INDEX'],1); assert m.confirm()==11 and m.clicks==[covers[1]]; passed()
m.clicks=[]; assert Machine.release(m)==11 and Machine.release(m,100)==0 and m.screens==[0] and not m.clicks; passed()
# The cover's click queries its tracks (staging restored) and lists them; a track hands playing_page
# our deque with its index as folder play (class 1, mode 2); a missing file refuses.
m.byte(syms['g_backlight_status'],1); f,ctx=m.handler(covers[1],O['EVT_CLICK'])
assert m.call(address=f,args=(ctx,m.event,0,0),gap=0)==0; m.advance(0)
assert m.query[:2]==('getMusicByAlbum','Album 1') and m.names(m.get(syms['tools_pdeq_directory']))==['staged']
rows=[w for w in m.nodes if m.nodes[w]['type']=='list_item' and m.alive(w)]; assert len(rows)==2
assert not m.nodes[m.get(m.slide+O['W_PARENT'])]['visible']
f,ctx=m.handler(rows[1],O['EVT_CLICK']); m.call(address=f,args=(ctx,m.event,0,0),gap=0)
assert len(m.plays)==1 and m.plays[0][0]=='playing_page' and m.plays[0][2:]==(1,1,2)
dq=m.plays[-1][1]&0xffffffff; assert m.names(dq)==['T1','T2']; passed()
m.missing=True; m.call(address=f,args=(ctx,m.event,0,0),gap=0); assert len(m.plays)==1 and 'Storage unavailable' in m.texts(); passed()
# Return: tracks -> covers on the same album, covers -> Home.
assert m.key()==11 and m.nodes[m.get(m.slide+O['W_PARENT'])]['visible'] and m.get(m.slide+O['SLIDE_INDEX'])==1 and not m.homes
assert m.key(O['KEY_NEXT'])==0 and m.key()==11 and m.homes==1; passed()
# The album list outlives the page: a reopen queries nothing until one of the songtable writers runs.
m=CoverflowMachine(); m.open(); m.close(); m.calls=[]
m.open(); assert not m.queried() and len(m.nodes[m.slide]['children'])==5; m.close(); passed()
for writer in WRITERS:
    m.rescan(writer); m.calls=[]; m.open(); assert m.queried()==1; m.close()
    m.calls=[]; m.open(); assert not m.queried(); m.close(); passed()
# Albums without art start the thread behind a progress screen; destroy joins it and drops the poll.
m=CoverflowMachine(cached=False); page=m.open()
assert len(m.threads)==1 and m.timers and any((t or '').startswith('Preparing artwork') for t in m.texts()) and 'Cancel' in m.texts()
f,ctx=m.handler(page,O['EVT_DESTROY']); assert m.call(address=f,args=(ctx,m.event,0,0),gap=0)==0
assert m.joins==[77] and not m.timers and m.freed==4; passed()  # three job paths and the job array
# Tracks list and play in album order (disc, track, then path, then CUE start), named without
# their file's extension; a CUE title keeps its dot.
def track(m,name,path,disc=0,no=0,cue=0):
    r=m.song(name); m.word(r+O['REC_PATH'],m.string(path))
    for k,v in (('REC_DISC',disc),('REC_TRACK',no),('REC_CUE_START',cue)): m.word(r+O[k],v)
    return r
for found,want in (((('b.mp3','/p/b.mp3',2,1),('a.mp3','/p/a.mp3',1,2),('c.mp3','/p/c.mp3',1,1)),['c','a','b']),
                   ((('Mr. Blue','/p/img.flac',0,0,300),('Intro','/p/img.flac'),('01 x.flac','/p/01 x.flac')),['01 x','Intro','Mr. Blue'])):
    m=CoverflowMachine(); m.found=[track(m,*t) for t in found]; m.open(); view=m.tracks()
    assert [m.nodes[m.nodes[i]['children'][0]].get('text') for i in m.nodes[view]['children']]==want
    f,ctx=m.handler(m.nodes[view]['children'][0],O['EVT_CLICK']); m.call(address=f,args=(ctx,m.event,0,0),gap=0)
    assert [n.rsplit('.',1)[0] if n.endswith(('.mp3','.flac')) else n for n in m.names(m.plays[-1][1]&0xffffffff)]==want; passed()

# Album holds use the live carousel before tracks have ever been opened, and the same ordering.
for action,want in ((0,['A','early','late','B','C']),(1,['A','B','C','early','late'])):
    m=CoverflowMachine(); m.found=[track(m,'late','/p/z',2,1),track(m,'early','/p/a',1,2)]
    m.open(); m.word(m.slide+O['SLIDE_INDEX'],2)
    m.press(100); assert m.hold()==11 and m.release()==0
    assert m.nodes[m.title]['text']=='Album 2' and m.labels()==['Play next','Add to queue','Shuffle','Go to artist']
    assert m.hold()==0
    m.pick(action); m.pick(action); m.advance(0)
    assert m.names()==want and not m.playback() and not m.plays and m.top==m.page
    assert m.query[:2]==('getMusicByAlbum','Album 2') and m.names(m.get(syms['tools_pdeq_directory']))==['staged']; passed()
for index in (3,4):
    m=CoverflowMachine(); m.open(); m.word(m.slide+O['SLIDE_INDEX'],index)
    m.press(100); assert m.hold()==0 and m.top==m.page; passed()
for kwargs in ({'albums':0},{'cached':False}):
    m=CoverflowMachine(**kwargs); m.open(); m.press(100); assert m.hold()==0; passed()
for invalidate in ('selection','closed','tracks'):
    m=CoverflowMachine(); m.open(); m.press(100); assert m.hold()==11; m.release()
    if invalidate=='selection': m.word(m.slide+O['SLIDE_INDEX'],1)
    elif invalidate=='closed': m.close()
    else: m.tracks(1)
    m.pick(0); m.advance(0)
    assert m.names()==['A','B','C'] and m.toasts[-1][3]=='Queue unchanged'; passed()
m=CoverflowMachine(); m.open(); m.tracks(2); m.key(); m.word(m.slide+O['SLIDE_INDEX'],1)
assert m.run(2) is None and m.plays[-1][2] in (0,1) and m.plays[-1][3:]==(1,2) and m.play_names==['T1','T2']; passed()
# Sorting leaves the utility card selected; the next album hold follows the reordered deque.
m=CoverflowMachine(); m.word(m.albums[2]+O['REC_ARTIST'],m.string('Aardvark')); m.open(); m.tracks(3); m.slide=m.find('slide_menu'); m.press(100); assert m.hold()==0
m.word(m.slide+O['SLIDE_INDEX'],0); m.press(101); assert m.hold()==11
assert m.nodes[m.title]['text']=='Album 2'; m.release(); m.pick(1); m.advance(0)
assert m.query[:2]==('getMusicByAlbum','Album 2'); passed()

# Go to artist uses the album record directly; an absent artist omits that action.
m=CoverflowMachine(); m.open(); m.run(3)
assert m.opened[-1][:2]==('localmusic/artistinfo_page',O['CLASS_ALBUMS']); passed()
m=CoverflowMachine(); m.word(m.albums[0]+O['REC_ARTIST'],0); m.open(); m.press(100); assert m.hold()==11
assert m.labels()==['Play next','Add to queue','Shuffle']; passed()
# Closing the source before the deferred open never creates a dialog.
m=CoverflowMachine(); m.open(); m.press(100)
assert m.call(O['KEY_CENTER'],address=syms['on_wm_keylong_fun'],event_type=O['EVT_KEY_LONG'],gap=0)==11
m.close(); m.advance(0); assert not any(p[0]=='dialog/sortselect_dialog' for p in m.opened); passed()

# Coverflow's own song deque feeds the shared queue menu, independently of stock browsing state.
for action,want in ((0,['A','T2','B','C']),(1,['A','B','C','T2'])):
    m=CoverflowMachine(cls=O['CLASS_ALBUMS']); m.open(); view=m.tracks()
    m.call(); assert m.selected(view)==1
    m.press(100); assert m.hold()==11 and m.nodes[m.title]['text']=='T2' and m.release()==0
    assert m.labels()==['Play next','Add to queue','Add to Favourites','Go to artist']
    assert m.hold()==0  # a repeated hold while the dialog is open cannot open another
    m.pick(action); m.pick(action); m.advance(0)
    assert m.names()==want and m.top==m.page and m.selected(view)==1 and not m.playback() and not m.plays
    assert m.names(m.get(syms['p_deque_showlist']))==['Row 0','Row 1','Row 2','Row 3']
    m.press(200); assert m.release()==1; passed()
# Its favourite is batch-select's over Coverflow's own deque, its songs taken as All Songs rows.
m=CoverflowMachine(cls=O['CLASS_ALBUMS']); m.open(); view=m.tracks(); assert m.run(2)=='Added to Favourites'
assert [c[1:3] for c in m.calls if c[0]=='batch_add_file']==[(0xf001,0xf00a)] and [c[1] for c in m.calls if c[0]=='batch_init_selectrecord']==[2]
assert [c[3] for c in m.calls if c[0]=='batch_add_file'][0]!=m.get(syms['p_deque_showlist']); passed()
m=CoverflowMachine(queue=0,cls=O['CLASS_ALBUMS']); m.open(); m.tracks()
assert m.run(1) is None and m.names()==['T1'] and m.mcl('MCL_TYPE')==1 and not m.playback(); passed()
m=CoverflowMachine(mode=2); m.open(); m.tracks(); m.run(0)
m.call(address=syms['mclNextSong'],args=(0,0,0,0),gap=0)
assert m.names()==['A','T1','B','C'] and m.mcl('MCL_POS')==1; passed()
# Return cancels without losing the highlighted song. Changed or closed sources refuse.
m=CoverflowMachine(); m.open(); view=m.tracks(); m.call(); m.press(100); m.hold(); m.release()
f,ctx=m.handler(m.top,O['EVT_KEY_UP']); ev=m.alloc(0x40); m.word(ev,O['EVT_KEY_UP']); m.word(ev+O['EVENT_KEY'],O['KEY_RETURN'])
assert m.call(address=f,args=(ctx,ev,0,0),gap=0)==11 and m.top==m.page
assert m.names()==['A','B','C'] and m.selected(view)==1 and not m.playback(); passed()
for invalidate in ('changed','closed','covers'):
    m=CoverflowMachine(); m.open(); m.tracks(); m.press(100); m.hold(); m.release()
    if invalidate=='changed':
        tracks=next(d for d in m.deqs if m.names(d)==['T1','T2'] and m.deqs[d][0]=='stSongInfo')
        m.word(m.items(tracks)[0]+O['REC_NAME'],m.string('changed'))
    elif invalidate=='closed': m.close()
    else: m.key()  # simulate the underlying page returning to covers before the action
    m.pick(0); m.advance(0)
    assert m.toasts[-1][3]=='Queue unchanged' and m.names()==['A','B','C'] and not m.playback(); passed()
# Album identity survives closing/reopening and library reordering; song positions are per album.
m=CoverflowMachine(); m.open()
m.call(address=syms['slide_menu_set_value'],args=(m.slide,2,0,0),gap=0)
f,ctx=m.handler(m.slide,O['EVT_VALUE_CHANGED']); m.call(address=f,args=(ctx,m.event,0,0),gap=0)
m.close(); m.albums.reverse(); m.rescan(); m.open()
assert m.get(m.slide+O['SLIDE_INDEX'])==0 and 'Album 2' in m.texts(); passed()
view=m.tracks(0); m.call(); assert m.selected(view)==1
m.key(); other=m.tracks(1); assert m.selected(other)==0
m.key(); view=m.tracks(0); assert m.selected(view)==1
m.close(); m.open(); view=m.tracks(0); assert m.selected(view)==1; passed()
# Long metadata and track titles use the looping stock scrolling label, with the complete text.
m=CoverflowMachine(); long='A very long title or artist name ' * 8
m.word(m.albums[0]+O['REC_ALBUM'],m.string(long)); m.word(m.albums[0]+O['REC_ARTIST'],m.string(long))
m.word(m.found[0]+O['REC_NAME'],m.string(long)); m.open(); assert m.texts().count(long)==2
m.tracks(); assert m.texts().count(long)==4  # covers stay alive, hidden behind the tracks
labels=[n for w,n in m.nodes.items() if m.alive(w) and n.get('text')==long]
assert all(n['type']=='hscroll_label' and n['loop']==1 and n['set_hscroll_label_attribute'] for n in labels); passed()
# Wheel only: the covers' slide_menu takes no touch.
m=CoverflowMachine(); m.open()
assert m.nodes[m.slide].get('sensitive')==0; passed()
# Text geometry. Both builds: album over artist under the covers' frame (CF_TEXT_Y), the album larger
# and white, the artist grey, CF_EDGE from the sides and clear of the rounded glass. iPod keeps every
# other label (the track list's title and rows, whose last visible row is lowest) CF_EDGE in too;
# Stock keeps its track list layout.
from ipod import corner_inset
def cf_geometry(m,w): return tuple(signed(m.get(w+O[k])) for k in ('W_X','W_Y','W_W','W_H'))
def clear(x,top,w,px):  # a label's text band, in screen rows (the window starts at y 30)
    return max(corner_inset(30+top),corner_inset(30+top+px))<=x and x+w<=375-max(corner_inset(30+top),corner_inset(30+top+px))
m=CoverflowMachine(); page=m.open(); labels=[w for w in m.nodes[m.get(m.slide+O['W_PARENT'])]['children'] if m.nodes[w]['type']=='hscroll_label']
name,artist=labels
E=O['CF_EDGE']; y=O['CF_TEXT_Y']
assert y>=O['CF_VIEW_H'] and y+O['CF_NAME_H']+O['CF_ARTIST_H']<=290
assert [cf_geometry(m,w) for w in labels]==[(E,y,375-2*E,O['CF_NAME_H']),(E,y+O['CF_NAME_H'],375-2*E,O['CF_ARTIST_H'])]
assert m.nodes[name]['style:normal:font_size']==O['CF_NAME_PX']==24 and m.nodes[artist]['style:normal:font_size']==O['CF_ARTIST_PX']==20
assert m.nodes[artist]['style:normal:text_color']==signed(O['CF_GREY']) and 'style:normal:text_color' not in m.nodes[name]
for w,px in ((name,O['CF_NAME_PX']),(artist,O['CF_ARTIST_PX'])):
    x,top,wd,h=cf_geometry(m,w); assert clear(x,top+(h-px)//2,wd,px)
if variant=='ipod':
    view=m.tracks(); lv=m.get(view+O['W_PARENT']); title=next(w for w in m.nodes[m.get(lv+O['W_PARENT'])]['children'] if m.nodes[w]['type']=='hscroll_label')
    assert cf_geometry(m,title)[::2]==(E,375-2*E) and clear(E,14,375-2*E,20)
    rows=(290-48)//48
    for item in m.nodes[view]['children']:
        label=m.nodes[item]['children'][0]; assert cf_geometry(m,label)[::2]==(E,375-2*E)
    assert clear(E,48+(rows-1)*48+14,375-2*E,20)  # the lowest visible row
else:
    view=m.tracks(); assert {cf_geometry(m,m.nodes[i]['children'][0])[::2] for i in m.nodes[view]['children']}=={(12,350)}
passed()

# Execute stock sibling selection and playback scheduling; mock filesystem enumeration,
# tag decoder and image codec boundaries, never the resulting player globals.
class RolloverMachine(CoverflowMachine):
    def __init__(self,sizes=(2,2,1,1)):
        super().__init__(queue=0,cls=1)
        self.native_trace={syms[n]:n for n in ('mclAutoChange','mclNextSong','on_player_autochange','toolsLoadNextDir',
                          'player_refresh_playqueue','mclLoadPlayList','player_get_id3info','player_set_coverinfo','mclClearChangeFlag')}
        self.trace=[]; self.files={}; self.dirs={}; self.artfiles=set(); self.worker=False
        self.handlers[0x5c29bc]='r:directory'; self.handlers[0x5c2c8c]='r:directory'
        self.handlers[0x5ace14]='r:decoder_start'
        # Lazy-bound malloc, as used by stock player_refresh_playqueue.
        self.word(0xa26cc0-0xabc,0x1000014); self.handlers[0x1000014]='r:malloc'
        for name in ('strcpy@GLIBC_2.0','strncmp@GLIBC_2.0','strstr@GLIBC_2.0','strchr@GLIBC_2.0',
                     'toolsGetMusicInfo','toolsGetFileSize','toolsGetAlbumCover','toolsGetExternCover',
                     'access@GLIBC_2.0','remove@GLIBC_2.0','usleep@GLIBC_2.0'):
            self.handlers[syms[name]]='r:'+name.split('@')[0]
        self.mock('pthread_mutex_lock@GLIBC_2.0','pthread_mutex_unlock@GLIBC_2.0','toolsFreeStSongInfo',
                  'mclLoadExLyric','sendBtHeadsetPlayStatus','notifyPlayInfo','notifyPlayStatus','notifyRefreshLyric',
                  'dmrNotifyPlayStatus','dlnaRenderSaveUrlMetadata','reset_repeatinfo','initializeDmrQCurrentInfo',
                  'player_reconfig','add_playrecord','toolsTrimLeft')
        self.handlers.pop(syms['mclStartPlayer'],None)
        for i,n in enumerate(sizes):
            folder=f'/mnt/mmc/{i}'; entries=[]
            for j in range(n):
                path=f'{folder}/{j}.flac'; r=self.song(f'{j}.flac')
                self.word(r+O['REC_PATH'],self.string(path)); self.word(r+0x38,1)
                for off in ('REC_ALBUM','REC_ARTIST'): self.word(r+O[off],0)
                entries.append(r); self.files[path]=(f'Album {i}' if i!=2 else '',i!=2)
            self.dirs[folder]=entries
        self.dirs['/mnt/mmc']=[]
        for folder in list(self.dirs)[:-1]:
            r=self.song(folder.rsplit('/',1)[1],4); self.word(r+O['REC_PATH'],self.string(folder)); self.dirs['/mnt/mmc'].append(r)
        self.word(0xa3bda8,syms['on_player_autochange']) # registered by stock player startup
        self.byte(0xa3be53,1) # mclSetJumpFolder's flag
        self.call(address=syms['mclLoadPlayList'],args=(self.deque(self.dirs['/mnt/mmc/0']),0,1,0),gap=0)
        self.call(address=syms['mclStartPlayer'],args=(0,0,0,0),gap=0)
        self.call(address=syms['player_get_id3info'],args=(0,0,0,0),gap=0)
    def hook(self,u,address,size,unused):
        if address in getattr(self,'native_trace',{}): self.trace.append((self.native_trace[address],None))
        name=self.handlers.get(address,'')
        if name=='widget_load_image':
            path=self.text(u.reg_read(UC_MIPS_REG_A1))
            self.image_size=None if path.startswith('file://') and path[7:] not in self.artfiles else (50,50)
        if not name.startswith('r:'): return super().hook(u,address,size,unused)
        name=name[2:]; a,b,c,d=[u.reg_read(r) for r in REGS]; ret=0
        self.trace.append((name,self.text(a) if name in ('directory','decoder_start','toolsGetAlbumCover') else a))
        if name=='directory':
            path=self.text(a); self.deqs[self.get(syms['tools_pdeq_directory'])][1]=[self.copy('stSongInfo',r) for r in self.dirs.get(path,[])]; ret=len(self.dirs.get(path,[]))
        elif name=='decoder_start': self.byte(0xa3be55,1)
        elif name=='malloc': ret=self.alloc((a+3)&~3)
        elif name=='strcpy': u.mem_write(a,self.text(b).encode()+b'\0'); ret=a
        elif name=='strncmp': x,y=self.text(a)[:c],self.text(b)[:c]; ret=(x>y)-(x<y)
        elif name in ('strchr','strstr'):
            i=self.text(a).find(chr(b) if name=='strchr' else self.text(b)); ret=a+i if i>=0 else 0
        elif name=='toolsGetMusicInfo':
            path=self.text(c); album,_=self.files[path]; self.trace.append(('tags',path))
            if album:
                u.mem_write(b,album.encode()+b'\0'); u.mem_write(b+0x100,b'Artist\0')
                u.mem_write(b+0x280,path.rsplit('/',1)[1].encode()+b'\0')
            ret=int(bool(album))
        elif name=='toolsGetFileSize': ret=4096
        elif name=='toolsGetAlbumCover':
            ret=int(self.files[self.text(a)][1])
            if ret: self.artfiles.add(self.text(b))
        elif name=='access': ret=0 if self.text(a) in self.artfiles else -1
        elif name=='remove': self.artfiles.discard(self.text(a))
        elif name=='usleep' and self.worker:
            u.reg_write(UC_MIPS_REG_PC,0x1000000); u.emu_stop(); return
        for r in [UC_MIPS_REG_V1,*REGS,UC_MIPS_REG_T8,UC_MIPS_REG_T9]: u.reg_write(r,0xdeadbeef)
        u.reg_write(UC_MIPS_REG_V0,ret&0xffffffff); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))
    def tick(self): self.call(address=syms['player_mainscheduling'],args=(0,0,0,0),gap=0)
    def artwork(self):
        self.worker=True
        self.u.reg_write(UC_MIPS_REG_SP,0x7000f000)
        self.u.reg_write(UC_MIPS_REG_T9,syms['player_parsecover_thd'])
        self.u.emu_start(syms['player_parsecover_thd'],0x1000000,count=self.budget)
        assert self.u.reg_read(UC_MIPS_REG_PC)==0x1000000
        self.worker=False


    def ui(self):
        self.top=self.win
        self.call(address=0x52bffc,args=(self.info,0,0,0),gap=0) # stock playing_timer_start callback
        if variant=='ipod':
            self.paint(self.win,gap=0)
            self.top=self.home; self.paint(self.home,gap=0); self.top=self.win
    def setup_ui(self):
        bar=self.node('window','system_bar'); self.word(syms['system_bar'],bar); self.word(bar+O['W_PARENT'],self.wm)
        if variant=='ipod':
            home_list(self); self.home=self.top; self.art=named(self,self.home,'img_homeart')
            self.call(address=home_hook[0],args=(self.home,0,0,0),gap=0)
        self.cover=self.node('image','img_cover'); self.songlabel=self.node('hscroll_label','scrlabel_title')
        self.albumlabel=self.node('label','label_ipod_album'); self.poslabel=self.node('label','label_ipod_pos')
        self.win=self.node('window','playing_page',[self.cover,self.songlabel,self.albumlabel,self.poslabel])
        self.word(self.win+O['W_PARENT'],self.wm); self.top=self.win
        self.info=self.alloc(0x40); self.word(self.info+0x20,self.win)
        if variant=='ipod':
            playing=IPOD_HOOKS['playing_page_init'][0]; self.handlers[playing+12]='stock_playing'
            self.call(address=playing,args=(self.win,0,0,0),gap=0)
        self.byte(syms['g_forcerefresh_flag'],1)

for sizes in ((2,2,1,1),(1,1,1,1)):
    m=RolloverMachine(sizes); m.setup_ui(); m.artwork(); m.ui()
    paths=[path for path in m.files]
    assert m.text(syms['g_play_id3_info'])==paths[0]
    for index,path in enumerate(paths[1:],1):
        old=m.text(syms['g_play_id3_info']); m.byte(0xa3be55,0) # hciplayer EOF
        # Delay the UI scheduler after stock queue replacement on alternate transitions.
        if index%2:
            m.call(address=syms['mclAutoChange'],args=(0,0,0,0),gap=0)
            assert m.text(syms['g_play_id3_info'])==old
            m.ui()
            if variant=='ipod':
                assert m.nodes[m.albumlabel]['text']=='' and m.nodes[m.art]['image']=='default_album_big'
        m.tick() # native auto-change, sibling traversal, queue reload, parsing and notification
        assert m.text(syms['g_play_id3_info'])==path
        assert m.text(syms['g_play_id3_info']+O['ID3_ALBUM'])==m.files[path][0]
        assert m.text(syms['g_play_cover_info']+8)==path
        assert m.text(syms['g_lastcover_url'])==old # worker has not completed yet
        m.ui()
        if variant=='ipod':
            assert m.nodes[m.albumlabel]['text']==m.files[path][0]
            assert m.nodes[m.art]['image']=='default_album_big'
        m.artwork()
        assert m.text(syms['g_lastcover_url'])==path
        assert m.u.mem_read(syms['g_playcover_finishflag'],1)==b'\1'
        m.ui()
        want='file:///tmp/coverpic.jpg' if m.files[path][1] else 'default_album_big'
        assert m.nodes[m.cover]['image']==(want if m.files[path][1] else 'play_defaultcover'),(path,m.nodes[m.cover])
        assert m.nodes[m.songlabel]['text']==path.rsplit('/',1)[1]
        if variant=='ipod':
            assert m.nodes[m.art]['image']==want
            assert m.nodes[m.poslabel]['text']==f"{m.mcl('MCL_POS')+1} of {len(m.names())}"
        queue=m.items(m.get(syms['mcl_pdeqplaylist']))
        assert all(not m.get(r+O['REC_ALBUM']) for r in queue) # tags came from parsing, not library rows
        assert ('tags',path) in m.trace and ('decoder_start',path) in m.trace
        passed()
    assert [path for event,path in m.trace if event=='directory']==['/mnt/mmc','/mnt/mmc/1','/mnt/mmc','/mnt/mmc/2','/mnt/mmc','/mnt/mmc/3']
    events=[event for event,_ in m.trace]
    for event in ('on_player_autochange','toolsLoadNextDir','player_refresh_playqueue'): assert events.count(event)==3
    assert events.count('mclLoadPlayList')==4 and events.count('mclClearChangeFlag')==len(paths)-1

# Coverflow depth (docs/internals.md#coverflow-depth): the payload's renderer, run as MIPS, draws
# byte for byte what the host build of the same source draws (test/coverflow.py checks that
# one's geometry); on the page it spans the frame, draws through the stock canvas and hit-tests taps.
BIG=0x2000000
def cover_pixels(seed):
    return b''.join(struct.pack('<I',0xff000000|((i*2654435761+seed*40503)>>7)&0xffffff) for i in range(160*160))
class DepthMachine(CoverflowMachine):
    budget=50_000_000  # decoding and rendering run in the payload, at native emulator speed (fast)
    def __init__(self,**kw):
        super().__init__(**kw)
        self.u.mem_map(BIG,0x800000); self.big=BIG
        self.frames=[]; self.destroyed=[]; self.draws=[]; self.loads=[]; self.unloads=0; self.pixels={}
        self.fast()
    def big_alloc(self,n):
        a=self.big; self.big+=(n+15)&~15; assert self.big<=BIG+0x800000; return a
    def hook(self,u,address,size,unused):
        name=self.handlers.get(address,'')
        a,b,c,d=[u.reg_read(r) for r in REGS]
        if name=='c:calloc@GLIBC_2.0' and a*b>0x10000: ret=self.big_alloc(a*b)
        elif name=='bitmap_create_ex':
            ret=self.big_alloc(0x48); data=self.big_alloc(b*c)
            self.word(ret,a); self.word(ret+4,b); self.word(ret+8,c); self.u.mem_write(ret+0xc,struct.pack('<HH',0,d)); self.word(ret+0x14,data)
            self.frames.append(ret)
        elif name=='bitmap_destroy': self.destroyed.append(a); ret=0
        elif name=='bitmap_lock_buffer_for_read': ret=self.get(a+0x14)
        elif name=='widget_load_image':
            url=self.text(b); self.loads.append(url) if a==self.top else None  # Coverflow's, not iPod Home's
            if url not in self.pixels:
                self.pixels[url]=self.big_alloc(160*160*4); self.u.mem_write(self.pixels[url],cover_pixels(len(self.pixels)))
            self.word(c,160); self.word(c+4,160); self.word(c+8,640); self.u.mem_write(c+0xc,struct.pack('<HH',2,1)); self.word(c+0x14,self.pixels[url]); ret=0
        elif name=='widget_unload_image': self.unloads+=a==self.top; ret=0
        elif name=='slide_menu_set_spacer': self.word(a+O['SLIDE_SPACER'],b); ret=0
        elif name=='canvas_draw_image':
            src,dst=[tuple(signed(self.get(r+4*i)) for i in range(4)) for r in (c,d)]
            line=self.get(b+8); data=self.get(b+0x14)
            frame=b''.join(bytes(self.u.mem_read(data+y*line,4*O['CF_VIEW_W'])) for y in range(O['CF_VIEW_H']))
            self.draws.append((a,b,src,dst,struct.unpack_from('<H',bytes(self.u.mem_read(b+0xc,2)))[0],frame)); ret=0
        else: return super().hook(u,address,size,unused)
        self.calls.append((name,a,b,c))
        for r in [UC_MIPS_REG_V1,*REGS,UC_MIPS_REG_T8,UC_MIPS_REG_T9]: u.reg_write(r,0xdeadbeef)
        u.reg_write(UC_MIPS_REG_V0,ret&0xffffffff); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))

def host_render(textures,cases):
    """The same renderer source built for the host: frames for (frac, mask of drawn slots) cases."""
    import subprocess, tempfile
    from coverflow import SHIM_H
    main=r"""#include <stdio.h>
void coverflow_render(unsigned *, int, int, const unsigned *const[7]);
static unsigned tex[7][160 * 160], frame[%d * %d];
int main(void) {
    const unsigned *ring[7];
    if (fread(tex, 4, 7 * 160 * 160, stdin) != 7 * 160 * 160) return 1;
    int frac, mask;
    while (scanf("%%d %%d", &frac, &mask) == 2) {
        for (int j = 0; j < 7; ++j) ring[j] = mask >> j & 1 ? tex[j] : 0;
        coverflow_render(frame, %d, frac, ring);
        fwrite(frame, 4, sizeof(frame) / 4, stdout);
        fflush(stdout);
    }
    return 0;
}""" % (O['CF_VIEW_H'],O['CF_VIEW_W'],O['CF_VIEW_W'])
    with tempfile.TemporaryDirectory(prefix='q2-render-') as d:
        d=pathlib.Path(d); (d/'shim.h').write_text(SHIM_H); (d/'main.c').write_text(main)
        subprocess.run(['cc','-m32','-O1','-DPEQ_HOST','-DPEQ_ROOT=""',f'-DIPOD={int(variant=="ipod")}','-D_GNU_SOURCE',
                        '-ffunction-sections','-fdata-sections','-Wl,--gc-sections','-I',str(ROOT/'patch'),'-include',str(d/'shim.h'),
                        str(ROOT/'patch/coverflow.c'),str(d/'main.c'),'-o',str(d/'render')],check=True)
        feed=b''.join(textures)+''.join(f'{f} {m}\n' for f,m in cases).encode()
        out=subprocess.run([str(d/'render')],input=feed,capture_output=True,check=True).stdout
    size=4*O['CF_VIEW_W']*O['CF_VIEW_H']
    return [out[i*size:(i+1)*size] for i in range(len(cases))]

ps=symbols(B/'patch.elf')
textures=[cover_pixels(100+j) for j in range(7)]
cases=[(0,127),(16384,127),(-16384,127),(32767,127),(-32768,127),(5000,0b0111110),(0,0)]
want=host_render(textures,cases)
m=DepthMachine(); tex=m.big_alloc(7*160*160*4); m.u.mem_write(tex,b''.join(textures))
ring=m.big_alloc(28); frame=m.big_alloc(4*O['CF_VIEW_W']*O['CF_VIEW_H'])
for (frac,mask),host in zip(cases,want):
    for j in range(7): m.word(ring+4*j,tex+j*160*160*4 if mask>>j&1 else 0)
    m.call(address=ps['coverflow_render'],args=(frame,O['CF_VIEW_W'],frac,ring),gap=0,count=50_000_000)
    assert bytes(m.u.mem_read(frame,len(host)))==host,(frac,mask)
    passed()

# On the page: the slide_menu spans the frame at CF_STRIDE per album, its children stay empty, and a
# paint of it draws the frame 1:1 at its origin, marked opaque; the covers around the position are
# decoded once, each load dropped at once. What is drawn is what the renderer draws for that ring.
m=DepthMachine(); page=m.open(); s=m.slide
assert len(m.frames)==1 and cf_geometry(m,s)==(0,0,O['CF_VIEW_W'],O['CF_VIEW_H'])
stride=signed(m.get(s+O['SLIDE_SPACER']))+O['CF_VIEW_H']; assert stride==O['CF_STRIDE']
assert all('image' not in m.nodes[c] for c in m.nodes[s]['children'])
def paint(m):
    return m.call(address=HOOKS['widget_on_paint_border'][0],args=(m.slide,m.canvas,0,0),gap=0,clear=False,count=50_000_000)
instructions=[0]
def count_block(u,address,size,unused): instructions[0]+=size//4
counter=m.u.hook_add(UC_HOOK_BLOCK,count_block,begin=BASE,end=SCRATCH-1)
paint(m); rest_cost=instructions[0]
assert len(m.draws)==1 and m.draws[0][1]==m.frames[0] and m.draws[0][2]==m.draws[0][3]==(0,0,O['CF_VIEW_W'],O['CF_VIEW_H'])
assert m.draws[0][4]&1  # BITMAP_FLAG_OPAQUE
# The placeholder first (fx_open), then the ring from -3 round album 0 of three and the Sort and
# Refresh cards: album 2, Sort and Refresh (the placeholder, no load), 0, 1, then 2 and Sort again.
assert len(m.loads)==4 and m.loads[0]=='default_album_big' and all(u.startswith('file://') for u in m.loads[1:]) and m.unloads==3
tex_of={u:bytes(m.u.mem_read(m.pixels[u],160*160*4)) for u in m.pixels}
ring_tex=[tex_of[m.loads[1]],tex_of['default_album_big'],tex_of['default_album_big'],tex_of[m.loads[2]],tex_of[m.loads[3]],tex_of[m.loads[1]],tex_of['default_album_big']]
assert host_render(ring_tex,[(0,127)])[0]==m.draws[0][5]; passed()
# A repaint in place draws again without rendering; a quarter turn renders from the live offset.
instructions[0]=0; paint(m); assert len(m.draws)==2 and instructions[0]<rest_cost//20
m.word(s+O['SLIDE_OFFSET'],-O['CF_STRIDE']//4); instructions[0]=0; paint(m); turn_cost=instructions[0]
assert m.draws[-1][5]!=m.draws[0][5] and len(m.loads)==4 and turn_cost>rest_cost//2; passed()
m.u.hook_del(counter)
m.word(s+O['SLIDE_OFFSET'],0); paint(m)
# The wheel's centre press clicks the centre child, which opens its tracks.
m.word(s+O['SLIDE_INDEX'],2); m.tracks(2)
assert m.query[:2]==('getMusicByAlbum','Album 2') and not m.nodes[m.get(s+O['W_PARENT'])]['visible']; passed()
# Closing releases the frame and the textures; allocation failure keeps the flat covers.
m.close(); assert m.destroyed==m.frames; passed()
class Starved(DepthMachine):
    def hook(self,u,address,size,unused):
        if self.handlers.get(address,'')=='bitmap_create_ex':
            u.reg_write(UC_MIPS_REG_V0,0); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA)); return
        return super().hook(u,address,size,unused)
m=Starved(); m.open(); s=m.slide
assert cf_geometry(m,s)==(0,24,375,160) and all(m.nodes[c].get('image') for c in m.nodes[s]['children'])
paint(m); assert not m.draws; passed()
print(f'Coverflow depth on MIPS: {rest_cost} instructions for the first paint (decoding the covers in reach, then the frame), {turn_cost} for a frame a quarter turn on (payload only; stock drawing mocked)')

if variant=='ipod':
    # Accent (docs/internals.md#accent). The mapping: stock red blended with a neutral becomes the same
    # blend of the preset's red tone, alpha kept; greys, other hues and every Crimson color stay.
    ps=symbols(B/'patch.elf'); RED=(0xff,0x14,0x48)
    # WCAG contrast of each preset: white text on the bar's top, the progress fill on its track, and
    # white on the red tone (Graphite's silver is chosen for its lit look instead; Crimson is stock).
    def lum(c):
        v=[(c>>s&255)/255 for s in (16,8,0)]; v=[x/12.92 if x<=0.04045 else ((x+0.055)/1.055)**2.4 for x in v]
        return 0.2126*v[0]+0.7152*v[1]+0.0722*v[2]
    def ratio(a,b): return (max(lum(a),lum(b))+0.05)/(min(lum(a),lum(b))+0.05)
    for i,(top,bottom,light,tone,_) in enumerate(ACCENTS):
        assert ratio(0xffffff,top)>=4.5 and ratio(0xffffff,bottom)>=4.5 and ratio(light,O['TRACK_COLOR'])>=3,i
        assert i in (0,O['CRIMSON']) or ratio(0xffffff,tone)>=3,i
    passed()
    def mapped(c,preset,tone=3): return Machine().call(address=ps['accent_map'],args=(c,preset,tone,0),gap=0)&0xffffffff
    def rgba(r,g,b,a=255): return r|g<<8|b<<16|a<<24
    def blend(t,k,to,a=255): return rgba(*(round(t*x+k) for x in to),a)
    def close(x,y): return x>>24==y>>24 and all(abs((x>>s&255)-(y>>s&255))<=2 for s in (0,8,16))
    for (preset,accent),column in ((p,c) for p in enumerate(ACCENTS) for c in (2,3)):  # light and red tones
        hi=(accent[column]>>16,accent[column]>>8&255,accent[column]&255)
        same=preset==O['CRIMSON']
        for c in (rgba(0,0,0),rgba(255,255,255),rgba(0x80,0x80,0x80),rgba(0x2b,0x2b,0x2b,0x40),  # greys
                  rgba(0xff,0x9f,0x0a),rgba(0x16,0x9a,0xa6),rgba(0xff,0,0xff),rgba(0x0a,0x84,0xff),  # other hues
                  *(color_t(c) for a in ACCENTS for c in a if a is not ACCENTS[1])):  # accents never map again
            assert mapped(c,preset,column)==c,(preset,hex(c))
        table=[(rgba(*RED),blend(1,0,hi)),                         # pure red
               (rgba(*RED,0x40),blend(1,0,hi,0x40)),               # translucent #FF144840: alpha kept
               (rgba(0x7f,0x0a,0x24),blend(0.5,0,hi)),             # red on black, half coverage
               (rgba(*(round((x+255)/2) for x in RED)),blend(0.5,127.5,hi)),  # anti-aliased onto white
               (rgba(0x3d,0x19,0x20),blend(0.153,21.6,hi))]        # the pressed tint
        for c,want in table:
            got=mapped(c,preset,column)
            assert (got==c) if same else close(got,want),(preset,hex(c),hex(got),hex(want))
            if not same: assert mapped(got,preset,column)==got
        passed()

    # Settings: IPOD/ACCENT, HOME and BATTERY come from the stock config.ini once, a missing or bad value
    # is the default; the style color hook returns stock values under Crimson and maps under others.
    red=color_t(0xff1448); grey=color_t(0x808080)
    tramp={n:int(manifest['patch_symbols'][f'stock_{n}_trampoline'],16) for n in ('color','image','display')}
    def style_color(m,stock,name='text_color'):
        m.handlers[tramp['color']]='stock_color'; out=m.alloc(4); m.stock_color=stock
        return m.call(address=IPOD_HOOKS['style_get_color'][0],args=(out,0x1234,m.string(name),0),gap=0)&0xffffffff,m.get(out),out
    orig_hook=Machine.hook; GET=0x1000600  # a style vtable's get_gradient
    def hook(self,u,address,size,x):
        if self.handlers.get(address)=='stock_color':
            a=u.reg_read(UC_MIPS_REG_A0); self.word(a,self.stock_color); self.calls.append(('stock_color',a))
            u.reg_write(UC_MIPS_REG_V0,a); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA)); return
        if address==GET and getattr(self,'get_gradient',None): return self.get_gradient(u)
        return orig_hook(self,u,address,size,x)
    Machine.hook=hook
    for config,preset in (({},0),({'ACCENT':'1'},1),({'ACCENT':'2'},2),({'ACCENT':'3','HOME':'1'},3),({'ACCENT':'7'},0),({'ACCENT':'12'},0),({'ACCENT':'4'},0)):  # 4 was Custom
        m=Machine(); m.config=config
        ret,got,out=style_color(m,red)
        assert ret==out and got==(red if preset==O['CRIMSON'] else color_t(ACCENTS[preset][3])),(config,hex(got))
        # Text takes the red tone, every other color property the light tone (Graphite: silver text,
        # #6E6E6E fills under white text).
        for name in ('highlight_text_color','bg_color','fg_color','border_color','selected_fg_color'):
            want=ACCENTS[preset][3 if name.endswith('text_color') else 2]
            assert style_color(m,red,name)[1]==(red if preset==O['CRIMSON'] else color_t(want)),(config,name)
        assert style_color(m,grey)[1]==grey
        assert len(m.config_reads)==3  # every key, once, on first use
        passed()
    m=Machine(); style_color(m,red)
    assert m.config_reads==[('/mnt/data/config.ini','IPOD',key,'0') for key in ('ACCENT','HOME','BATTERY')]; passed()

    # Gradients: the leaf's null checks, then the caller's stops mapped (nr @8, stops @0xc).
    def gradient(config,stops,same_out=True,vt_get=True,style=True):
        m=Machine(); m.config=config
        out=m.alloc(0x4c); other=m.alloc(0x4c); vt=m.alloc(0x20); st=m.alloc(8)
        m.word(st,vt); m.word(vt+0x18,GET if vt_get else 0)
        def get(u):
            assert u.reg_read(UC_MIPS_REG_T9)==GET and u.reg_read(UC_MIPS_REG_A0)==st and u.reg_read(UC_MIPS_REG_A2)==out
            g=out if same_out else other
            m.word(g+8,len(stops))
            for i,c in enumerate(stops): m.word(g+0xc+8*i,c); m.word(g+0x10+8*i,i)
            u.reg_write(UC_MIPS_REG_V0,g); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))
        m.get_gradient=get
        ret=m.call(address=ps['ringnav_style_gradient'],args=(st if style else 0,0,out,0),gap=0)&0xffffffff
        g=out if same_out else other
        return ret,[m.get(g+0xc+8*i) for i in range(len(stops))],out,other
    ret,cols,out,_=gradient({},[red,grey,color_t(0x7f0a24)])
    assert ret==out and cols==[color_t(ACCENTS[0][2]),grey,mapped(color_t(0x7f0a24),0,2)]; passed()
    assert gradient({'ACCENT':'1'},[red,grey])[1]==[red,grey]; passed()
    ret,cols,_,other=gradient({},[red],same_out=False); assert ret==other and cols==[red]; passed()
    assert gradient({},[red],vt_get=False)[0]==0 and gradient({},[red],style=False)[0]==0; passed()
    # Through stock style_get_color, which asks style_get_gradient first for every color: its call
    # is left unmapped, so the color hook still gives text the red tone and fills the light tone.
    for name,column in (('text_color',3),('bg_color',2)):
        m=Machine(); vt=m.alloc(0x20); st=m.alloc(8); m.word(st,vt); m.word(vt+0x14,0x1000700); m.word(vt+0x18,GET)
        def get(u):
            g=u.reg_read(UC_MIPS_REG_A2); m.word(g+8,1); m.word(g+0xc,red)
            u.reg_write(UC_MIPS_REG_V0,g); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))
        m.get_gradient=get; out=m.alloc(4)
        m.call(address=IPOD_HOOKS['style_get_color'][0],args=(out,st,m.string(name),0),gap=0)
        assert m.get(out)==color_t(ACCENTS[0][column]),(name,hex(m.get(out))); passed()

    # Images: decoded 32-bit theme bitmaps are mapped in place before stock caches them; covers by
    # path or URL, Crimson and other formats are left alone. bitmap_t w @0, h @4, line_length @8, format @0xe.
    def image(config,name,fmt=3):
        m=Machine(); m.config=config; m.handlers[tramp['image']]='stock_image'
        bm=m.alloc(0x60); data=m.alloc(64)
        m.word(bm,2); m.word(bm+4,2); m.word(bm+8,12); m.u.mem_write(bm+0xe,struct.pack('<H',fmt)); m.word(bm+0x14,data)
        px=[bytes([0x48,0x14,0xff,0x80]),bytes([0x80,0x80,0x80,0xff])]  # BGRA: translucent red, grey
        for y in range(2): m.u.mem_write(data+12*y,px[0]+px[1]+b'\xee'*4)  # 4 bytes of row padding
        ret=m.call(address=IPOD_HOOKS['image_manager_add'][0],args=(0x1000500,m.string(name),bm,0),gap=0)
        assert ret==0 and [c[1:] for c in m.calls if c[0]=='stock_image'][0][2]==bm
        return [bytes(m.u.mem_read(data+12*y,12)) for y in range(2)]
    hi=ACCENTS[0][3]; want=bytes([hi&255,hi>>8&255,hi>>16,0x80])+bytes([0x80,0x80,0x80,0xff])+b'\xee'*4
    assert image({},'switch_on')==[want,want]; passed()
    stock=bytes([0x48,0x14,0xff,0x80,0x80,0x80,0x80,0xff])+b'\xee'*4
    for config,name,fmt in (({'ACCENT':'1'},'switch_on',3),({},'file:///tmp/coverpic.jpg',3),({},'/mnt/mmc/a/cover.jpg',3),
                            ({},'https://resources.tidal.com/images/a/320x320.jpg',3),({},'switch_on',5)):
        assert image(config,name,fmt)==[stock,stock],(config,name,fmt); passed()

    # The confirm pop-up's red discs take the dark CONFIRM_SURFACE under every accent, Crimson too:
    # the stock OK disc (#FF1448, white glyph) and Cancel's tint (#FF4871 with a #FFE4EA glyph) keep
    # their glyphs at 4.5:1 or more; the pressed images darken; other images keep the accent's tone.
    def pixels(config,name,colors):
        m=Machine(); m.config=config; m.handlers[tramp['image']]='stock_image'
        bm=m.alloc(0x60); data=m.alloc(4*len(colors))
        m.word(bm,len(colors)); m.word(bm+4,1); m.word(bm+8,4*len(colors)); m.u.mem_write(bm+0xe,struct.pack('<H',3)); m.word(bm+0x14,data)
        m.u.mem_write(data,b''.join(bytes([c&255,c>>8&255,c>>16,255]) for c in colors))  # BGRA
        m.call(address=IPOD_HOOKS['image_manager_add'][0],args=(0x1000500,m.string(name),bm,0),gap=0)
        raw=bytes(m.u.mem_read(data,4*len(colors)))
        return [raw[4*i+2]<<16|raw[4*i+1]<<8|raw[4*i] for i in range(len(colors))]
    S=O['CONFIRM_SURFACE']
    for preset in range(len(ACCENTS)):
        config={'ACCENT':str(preset)}
        disc,glyph=pixels(config,'confirm_ok',[0xff1448,0xffffff])
        assert disc==S and glyph==0xffffff and ratio(glyph,disc)>=4.5,(preset,hex(disc))
        disc,glyph=pixels(config,'confirm_cancel',[0xff4871,0xffe4ea])
        assert ratio(glyph,disc)>=4.5 and ratio(disc,0)<ratio(glyph,0),(preset,hex(disc),hex(glyph))
        assert pixels(config,'confirm_okdown',[0x7f0a24])[0]<S  # pressed: darker than the disc
        assert pixels(config,'switch_on',[0xff1448])==[0xff1448 if preset==O['CRIMSON'] else ACCENTS[preset][3]]
        passed()

    # Quick settings: the active controls' stock red discs (#FF1448, white glyph, pink anti-aliased
    # glyph edges) take the accent's red tone, as everything red does; on a tone brighter than
    # GLYPH_LIGHT_MAX (Graphite's silver) the glyph turns CONFIRM_SURFACE so it stays legible, else
    # it stays white. Glyph and edges keep 3:1 on the disc and transparent pixels are untouched.
    # Inactive (#444444) and disabled discs keep their stock greys, and an active disc reads clearly
    # apart from them. The brightness suns, on black rather than a disc, keep the accent's red tone.
    def rgba(config,name,pixels_):
        m=Machine(); m.config=config; m.handlers[tramp['image']]='stock_image'
        bm=m.alloc(0x60); data=m.alloc(4*len(pixels_))
        m.word(bm,len(pixels_)); m.word(bm+4,1); m.word(bm+8,4*len(pixels_)); m.u.mem_write(bm+0xe,struct.pack('<H',3)); m.word(bm+0x14,data)
        m.u.mem_write(data,b''.join(bytes([c&255,c>>8&255,c>>16&255,a]) for c,a in pixels_))  # BGRA
        m.call(address=IPOD_HOOKS['image_manager_add'][0],args=(0x1000500,m.string(name),bm,0),gap=0)
        raw=bytes(m.u.mem_read(data,4*len(pixels_)))
        return [(raw[4*i+2]<<16|raw[4*i+1]<<8|raw[4*i],raw[4*i+3]) for i in range(len(pixels_))]
    ACTIVE=('drop_wifiopen','drop_btopen','drop_keylockopen','drop_highgain','drop_lo','drop_usbaudio','drop_usbdac')
    INACTIVE=('drop_wifi','drop_bt','drop_keylock','drop_lowgain','drop_po','drop_usbstorage','drop_playset','drop_sysset')
    disc=[(0xff1448,255),(0xffffff,255),(0xffc0d0,255),(0xff1448,0),(0x000000,0)]  # disc, glyph, glyph edge, transparent
    def perceived(c): return ((c>>16)*299+(c>>8&255)*587+(c&255)*114)//1000
    for preset in range(len(ACCENTS)):
        config={'ACCENT':str(preset)}
        tone=0xff1448 if preset==O['CRIMSON'] else ACCENTS[preset][3]
        light=preset!=O['CRIMSON'] and perceived(tone)>O['GLYPH_LIGHT_MAX']
        for name in ACTIVE:
            (d,da),(g,ga),(e,ea),*clear=rgba(config,name,disc)
            assert d==tone and da==255 and g==(S if light else 0xffffff),(preset,name,hex(d),hex(g))
            assert ratio(g,d)>=3 and ratio(e,d)>=2,(preset,name,hex(d),hex(e))
            assert max(ratio(d,0x444444),ratio(0x444444,d))>=1.5,(preset,name,hex(d))  # reads apart from inactive
            assert [a for _,a in clear]==[0,0],(preset,name)  # transparency kept
        assert light==(preset==0)  # only Graphite's silver takes the dark glyph
        for name in INACTIVE+('drop_highgaindisable','drop_lowgaindisable'):
            grey=[(0x444444,255),(0xffffff,255),(0x5b5b5b,255),(0x222222,255)]
            assert rgba(config,name,grey)==grey,(preset,name)
        red=[(0xff1448,255)]
        for name in ('drop_lighleft','drop_lightright','eqdrop_dot','dropdown','xdrop_bt'):
            assert rgba(config,name,red)==[(tone,255)],(preset,name)
        # Red under white is a surface and takes the light tone, so the white stays legible (4.5:1
        # on Graphite, not white on silver): a switch's knob, a disc's glyph, or the label over a
        # btn_ image, which holds no white itself. A red mark alone (a tick, the knob-less pixels
        # of the tests above) keeps the red tone, and transparent white is not a glyph.
        lit=0xff1448 if preset==O['CRIMSON'] else ACCENTS[preset][2]
        for name in ('switch_on','add','dec','mulselect'):
            assert rgba(config,name,disc[:2])==[(lit,255),(0xffffff,255)] and ratio(0xffffff,lit)>=3,(preset,name)
            assert rgba(config,name,[(0xffffff,255),(0xff1448,255)])==[(0xffffff,255),(lit,255)],(preset,name)
        for name in ('btn_enter','btn_red'):
            assert rgba(config,name,red)==[(lit,255)],(preset,name)
        # Pressed images (#7F0A24, a disc's glyph #7F7F7F) follow: darker than the same red alone.
        alone=rgba(config,'select',[(0x7f0a24,255)])[0][0]
        for name,px in (('btn_enterdown',[(0x7f0a24,255)]),('add_down',[(0x7f0a24,255),(0x7f7f7f,255)])):
            dim=rgba(config,name,px)
            assert dim[1:]==px[1:] and (dim[0][0]<alone if preset==0 else dim[0][0]==alone),(preset,name,hex(dim[0][0]))
        for name in ('select','sleepdot'):
            assert rgba(config,name,red+[(0xffffff,0)])==[(tone,255),(0xffffff,0)],(preset,name)
        # The settings rows' category icons keep their stock colours, red ones included.
        pink=[(0xcf2f53,255),(0xff1448,255),(0xffffff,255)]
        for name in ('system_netservice','netservice_dlna','wifiset_wifi','display_backlight','system_language'):
            assert rgba(config,name,pink)==pink,(preset,name)
        # A cover whose file name starts like an asset stays unmapped.
        assert rgba(config,'file:///mnt/mmc/drop_bt.png',red)==red
        passed()

    # Display settings: after the stock rows, Accent, Home and Battery rows in the native row widgets
    # and styles, with the Display, cover mode and power manager icons; Centre or tap cycles and saves
    # each; a new accent drops the image cache and repaints, a new Battery mode repaints the bar.
    def display(config):
        CONFIG.clear(); CONFIG.update(config); m=QueueMachine(); m.handlers[tramp['display']]='stock_display'
        view=m.node('scroll_view','scroll_view_display',[m.entry(0) for _ in range(3)])
        for e in m.nodes[view]['children']: m.word(e+O['W_PARENT'],view)
        m.top=m.node('window','display_page',[m.node('list_view','list_view_display',[view])])
        assert m.call(address=IPOD_HOOKS['systemset_display_page_init'][0],args=(m.top,5,0,0),gap=0)==0
        assert m.calls[0][:3]==('stock_display',m.top,5)
        rows=m.nodes[view]['children'][3:]
        m.icons_set=[m.text(c[2]) for c in m.calls if c[0]=='image_base_set_image']
        return m,view,rows
    m,view,rows=display({})
    assert len(rows)==3 and m.icons_set==['system_display','playset_covermode','system_powermanager'] and all(m.nodes[r]['type']=='list_item' and m.nodes[r]['style']=='s_listitem_black' for r in rows)
    buttons=[m.nodes[r]['children'][0] for r in rows]; labels=[m.nodes[b]['children'][1] for b in buttons]
    for b,l in zip(buttons,labels):
        icon=m.nodes[b]['children'][0]  # stock's 0x4c19bc icon geometry, which the row layouter maps
        assert m.nodes[icon]['type']=='image' and [m.get(icon+O[k]) for k in ('W_X','W_Y','W_W','W_H')]==[10,0,52,70]
        assert m.nodes[b]['style']=='s_btn_listitem' and [m.get(b+O[k]) for k in ('W_X','W_Y','W_W','W_H')]==[20,0,335,70]
        assert m.nodes[l]['type']=='hscroll_label' and m.nodes[l]['style']=='s_scrlabel_white24l' and m.get(l+O['W_X'])==72
    def texts(): return [m.nodes[l]['text'] for l in labels]
    assert texts()==['Accent: Graphite','Home: Split','Battery: Icon']; passed()
    def click(i):
        m.calls=[]; f,ctx=m.handler(buttons[i],O['EVT_CLICK'])
        assert m.call(address=f,args=(ctx,m.event,0,0),gap=0)==0
        return [c[1:] for c in m.calls if c[0]=='write_int_config']
    for value,name in ((1,'Crimson'),(2,'Tidal'),(3,'Champagne'),(0,'Graphite')):
        writes=click(0)
        assert len(writes)==1 and writes[0][0]==value and m.text(writes[0][1])=='IPOD' and m.text(writes[0][2])=='ACCENT'
        assert ('image_manager_unload_all',0x1000500) in [c[:2] for c in m.calls] and ('widget_invalidate_force',m.wm) in [c[:2] for c in m.calls]
        assert texts()[0]=='Accent: '+name; passed()
    writes=click(1); assert [(w[0],m.text(w[2])) for w in writes]==[(1,'HOME')] and texts()[1]=='Home: Full'
    assert not [c for c in m.calls if c[0]=='image_manager_unload_all']; passed()
    click(1); assert texts()[1]=='Home: Split'; passed()
    bar=m.node('window','system_bar'); m.word(syms['system_bar'],bar)
    for value,name in ((1,'Percent'),(2,'Icon + Percent'),(0,'Icon')):
        writes=click(2); assert [(w[0],m.text(w[2])) for w in writes]==[(value,'BATTERY')] and texts()[2]=='Battery: '+name
        assert ('widget_invalidate_force',bar) in [c[:2] for c in m.calls] and not [c for c in m.calls if c[0]=='image_manager_unload_all']
    passed()
    m,view,rows=display({'ACCENT':'2','HOME':'1','BATTERY':'2'}); got=[m.nodes[m.nodes[m.nodes[r]['children'][0]]['children'][1]]['text'] for r in rows]; assert got==['Accent: Tidal','Home: Full','Battery: Icon + Percent']; passed()
    # The wheel walks onto the new rows and Centre clicks them, as any fixed settings list.
    m,view,rows=display({})
    m.paint(view)
    for _ in range(3): m.call()
    assert m.selected(view)==3 and m.confirm()==11 and m.dispatched()[0][1]==m.nodes[rows[0]]['children'][0]; passed()
    # PEQ's value edit has action_text "done": stock edit_on_event reports the keyboard's OK only for it (or "next").
    stock=(B/'stock-demo').read_bytes()
    def at(va,n): return next(stock[o+va-v:o+va-v+n] for _,(t,o,v,_,f,*_) in segments(stock) if t==1 and v<=va<v+f)
    assert at(0x613ee8,4)==bytes.fromhex('1cc7a524') and at(0x77c71c,5)==b'done\0'; passed()  # edit_on_event: strcmp(action_text, "done")

    # Settings rows (docs/ipod.md#settings). The real stock builders create their rows; the build
    # points the list_view layouter's vtable slot at ipod_list_layout, which normalises stock 78px
    # items, runs the stock layout (stacking modelled here: item_height, else the item's own height,
    # else default_item_height, as 0x5ea5c4 onward) and maps each row's children.
    from ipod import corner_inset
    class SettingsMachine(Machine):
        def hook(self,u,address,size,x):
            name=self.handlers.get(address,'')
            if name=='stock_list_layout':
                assert u.reg_read(UC_MIPS_REG_T9)==address
                view=u.reg_read(UC_MIPS_REG_A1); lst=self.get(view+O['W_PARENT']); y=0
                ih,dh=(self.get(lst+O[k]) for k in ('ROW_HEIGHT','LIST_DEFAULT_ITEM_HEIGHT'))
                for c in self.nodes[view]['children']:
                    if not self.get(c+O['W_W']): self.word(c+O['W_W'],self.get(view+O['W_W']))
                    h=ih or self.get(c+O['W_H']) or dh
                    self.word(c+O['W_Y'],y); self.word(c+O['W_H'],h); y+=h
                self.word(view+O['VIEW_CONTENT_H'],y); self.layouts+=1
                u.reg_write(UC_MIPS_REG_V0,0); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA)); return
            if name=='widget_on' and u.reg_read(UC_MIPS_REG_A1)==O['EVT_CLICK']:  # a clickable row
                a=u.reg_read(UC_MIPS_REG_A0); em=self.alloc(4); it=self.alloc(0x28)
                self.word(a+O['W_EMITTER'],em); self.word(em,it); self.word(it+O['EMIT_TYPE'],O['EVT_CLICK'])
            if name=='image_set_draw_type': self.nodes[u.reg_read(UC_MIPS_REG_A0)]['draw_type']=u.reg_read(UC_MIPS_REG_A1)
            return super().hook(u,address,size,x)
    SET={k:O['SET_'+k] for k in ('ROW','TOP','ROWS','ICON','ICON_X','GAP','TEXT_X','EDGE','STOCK_ROW')}
    def geometry(m,w): return tuple(signed(m.get(w+O[k])) for k in ('W_X','W_Y','W_W','W_H'))
    def settings(builder,default=SET['ROW']):
        m=SettingsMachine(); m.layouts=0
        m.mock('list_item_create','button_create','image_create','hscroll_label_create','label_create','widget_use_style',
               'widget_set_name','image_set_draw_type','image_base_set_image','set_hscroll_label_attribute','widget_on',
               'widget_set_text_utf8','widget_set_visible','widget_move_resize')
        m.handlers[syms['widget_destroy_children']]='widget_destroy_children'
        for n in syms:  # string helpers the builders format their names with
            if n.endswith('@GLIBC_2.0') and syms[n] not in m.handlers: m.handlers[syms[n]]=n
        m.handlers[O['LIST_VIEW_LAYOUT']]='stock_list_layout'
        view=m.node('scroll_view'); lst=m.node('list_view','list_view',[view]); m.top=m.node('window','sysset_page',[lst])
        m.word(view+O['W_PARENT'],lst); m.word(lst+O['W_PARENT'],m.top); m.word(m.top+O['W_PARENT'],m.wm)
        m.word(lst+O['ROW_HEIGHT'],0); m.word(lst+O['LIST_DEFAULT_ITEM_HEIGHT'],default)
        m.word(view+O['W_W'],375); m.word(view+O['W_H'],SET['ROWS']*SET['ROW'])
        if builder=='display':  # the payload's Accent and Home rows, after three stock-shaped ones
            m.handlers[tramp['display']]='stock_display'; m.nodes[view]['name']='scroll_view_display'
            assert m.call(address=IPOD_HOOKS['systemset_display_page_init'][0],args=(m.top,5,0,0),gap=0)==0
        else:
            m.call(address=builder,args=(m.top,0,0,0),gap=0)  # learn the name it looks up
            m.nodes[view]['name']=m.text(next(c for c in m.calls if c[0]=='widget_lookup')[2])
            assert m.call(address=builder,args=(m.top,0,0,0),gap=0)==0
        return m,view
    def lay(m,view): return m.call(address=m.get(O['LIST_VIEW_LAYOUT_SLOT']),args=(0x1234,view,0,0),gap=0)
    def tree(m,view):
        return [(geometry(m,i),[(geometry(m,b),[geometry(m,c) for c in m.nodes[b]['children']]) for b in m.nodes[i]['children']])
                for i in m.nodes[view]['children']]
    # Rows at the top and bottom of the four visible slots: text bands, icons and the trailing
    # bitmaps' ink (switch_off/on span 0..50 x 13..38 of their 50px square; list_into and ticks fit
    # inside) stay clear of the rounded glass, and nothing overlaps.
    def check_row(m,item,slot):
        top=30+SET['TOP']+slot*SET['ROW']
        for b in m.nodes[item]['children']:
            bx,by,bw,bh=geometry(m,b)
            assert (bx,by,bw,bh)==(0,0,375,geometry(m,item)[3])
            icon=label=trail=None
            for c in m.nodes[b]['children']:
                x,y,w,h=geometry(m,c); kind=m.nodes[c]['type']
                if kind=='image' and w==SET['ICON']:
                    icon=(x,y,w,h); box=(x,top+y,w,h)
                    assert (x,h)==(SET['ICON_X'],SET['ICON']) and y*2+h==bh and m.nodes[c]['draw_type']==O['IMAGE_DRAW_SCALE_DOWN']
                elif kind=='image':
                    assert w==50 and x+w==375-SET['EDGE'], (x,w); trail=x; box=(x,top+y+(h-50)//2+13,50,25)
                else:
                    label=(x,w); size=20 if h<30 else 24
                    box=(x,top+y+(h-size)//2,w,size)
                x,y,w,h=box; inset=max(corner_inset(y),corner_inset(y+h))
                assert inset<=x and x+w<=375-inset, (m.nodes[c]['type'],box,inset)
            if icon and label: assert label[0]==icon[0]+icon[2]+SET['GAP']
            elif label: assert label[0]==SET['TEXT_X']
            if label and not trail: assert label[0]+label[1]==375-SET['TEXT_X']
            if label and trail: assert label[0]+label[1]<=trail+30  # the chevron's glyph starts 20px in
    for builder in (0x4c43e4, 0x4c0f6c, 0x4cbcc4, 0x4ccc70, 'display'):  # language, BT quality, System settings, Wi-Fi, Display
        m,view=settings(builder); items=m.nodes[view]['children']
        before=tree(m,view); assert lay(m,view)==0 and m.layouts==1
        assert [geometry(m,i)[1] for i in items]==[SET['ROW']*k for k in range(len(items))], builder  # 78px items too
        assert all(geometry(m,i)[3]==SET['ROW'] for i in items)
        for item in items: check_row(m,item,0); check_row(m,item,SET['ROWS']-1)
        after=tree(m,view); assert after!=before
        assert lay(m,view)==0 and tree(m,view)==after, builder  # a later layout changes nothing
        passed()
    # A list whose default_item_height is not SET_ROW (local pages, Home) keeps its rows as built.
    m,view=settings(0x4cbcc4,default=72); before=tree(m,view); lay(m,view); after=tree(m,view)
    assert [r[1] for r in after]==[r[1] for r in before] and all(r[0][3]==72 for r in after); passed()
    # A 78px item some other builder made (no stock settings button) keeps its height and children.
    m,view=settings(0x4cbcc4); odd=m.node('list_item'); other=m.node('button')
    m.nodes[odd]['children']=[other]; m.word(odd+O['W_H'],78); m.word(odd+O['W_W'],0)
    for k,v in (('W_X',8),('W_Y',0),('W_W',359),('W_H',70)): m.word(other+O[k],v)
    m.nodes[view]['children'].append(odd); lay(m,view)
    assert geometry(m,odd)[3]==78 and geometry(m,other)==(8,0,359,70); passed()
    # The wheel walks the laid-out rows with hard ends, the bar spans the whole 68px row, the list
    # scrolls to keep the row in view, and Centre and a tap reach the same button.
    m,view=settings(0x4cbcc4); lay(m,view); items=m.nodes[view]['children']
    buttons=[m.nodes[i]['children'][0] for i in items]
    m.paint(view); assert m.selected(view)==0
    for k in range(1,len(items)+2):
        m.call(); m.paint(view); i=min(k,len(items)-1); assert m.selected(view)==i
        y=geometry(m,items[i])[1]-signed(m.get(view+O['SCROLL_Y']))
        assert 0<=y and y+SET['ROW']<=SET['ROWS']*SET['ROW'], (k,y)
        assert len(m.bands)==SET['ROW']+1 and {b[2] for b in m.bands}=={375}
    assert m.confirm()==11 and m.dispatched()[0][1]==buttons[-1]; passed()
    m.click(buttons[2]); assert m.selected(view)==2; passed()

    # The payload's own accent drawing follows the preset, live, including Now Playing's fill.
    m,view,rows=display({'ACCENT':'2'}); m.paint(view)
    assert [m.bands[i][4] for i in (0,47,48)]==[color_t(ACCENTS[2][i]) for i in (0,1,4)]; passed()
    f,ctx=m.handler(m.nodes[rows[0]]['children'][0],O['EVT_CLICK']); m.call(address=f,args=(ctx,m.event,0,0),gap=0)
    m.paint(view); assert [m.bands[i][4] for i in (0,47,48)]==[color_t(ACCENTS[3][i]) for i in (0,1,4)]; passed()
    CONFIG.clear(); m=np_page()
    assert m.nodes[m.slider][FG]==signed(color_t(ACCENTS[0][2])); passed()
    CONFIG.update(ACCENT='3'); m=QueueMachine(queue=3,pos=1); m.handlers[playing+12]='stock_playing'
    m.slider=m.node('slider','slider_play',max=225,value=100)
    m.win=m.top=m.node('window','playing_page',[m.slider,m.node('label','label_playtime')]); m.word(m.win+O['W_PARENT'],m.wm)
    m.call(address=playing,args=(m.win,7,0,0),gap=0)
    assert m.nodes[m.slider][FG]==signed(color_t(ACCENTS[3][2])); passed()

    # Home: Full widens the list and its rows' tap targets to the screen and hides the art at init
    # and when the setting changes; Split restores the asset's width.
    CONFIG.clear(); CONFIG.update(HOME='1'); m=CoverflowMachine(); m.open()
    rowsw=[m.get(w+O['W_W']) for w in [m.list,*m.imgs]]
    assert rowsw==[375]+[O['HOME_FULL_ROW']]*7 and m.nodes[m.art]['visible']==0; passed()
    CONFIG.clear(); m=CoverflowMachine(); m.open(); assert m.get(m.list+O['W_W'])==HOME_LIST_W and m.nodes[m.art].get('visible',1); passed()
    Machine.hook=orig_hook; CONFIG.clear()

# Wheel precision (docs/internals.md, Wheel movement): iPod's overshoot filter (the second tick of a
# run is dropped when it comes OVERSHOOT_MIN_MS to OVERSHOOT_MAX_MS after the first), the list
# ends, and Key Tone moved from the press to the row change.
KEYDOWN=IPOD_HOOKS['on_wm_keydown_before_fun'][0]
NEXT,PREV,CENTER,RETURN=O['KEY_NEXT'],O['KEY_PREV'],O['KEY_CENTER'],O['KEY_RETURN']
TONE=syms['g_keytone_flag']; SOUND_LATCH=0xa37c90; KEY_LATCH=0xa37c89  # stock: one click a press; wheel lockout
def audible(m,tone=1):
    """Key Tone as on the device: the real stock buzzeer_switch runs, and only the system() command it
    hands the MCU, the hardware boundary, is mocked."""
    m.handlers.pop(syms['buzzeer_switch'],None); m.mock('system@GLIBC_2.0'); m.byte(TONE,tone); return m
def buzzes(m): return sum(c[0]=='system@GLIBC_2.0' and m.text(c[1])=='cmd_mcu write_str buzzer' for c in m.calls)
def press(m,key=NEXT,gap=1000):
    """The window manager's key-down-before through the stock entry (iPod: the hook), leaving the stock
    latches as stock does. Returns its buzzer commands; the Key Tone byte must come back as it was."""
    tone=bytes(m.u.mem_read(TONE,1))
    m.down=m.call(key,address=KEYDOWN,event_type=O['EVT_KEY_DOWN_BEFORE'],gap=gap,debounce=True)
    assert bytes(m.u.mem_read(TONE,1))==tone
    return buzzes(m)
def lift(m,key=NEXT):
    """Its key-up-before, at once; m.ret is the hook's result."""
    tone=bytes(m.u.mem_read(TONE,1))
    m.ret=m.call(key,event_type=O['EVT_KEY_UP_BEFORE'],gap=0,debounce=True)
    assert bytes(m.u.mem_read(TONE,1))==tone and m.u.mem_read(SOUND_LATCH,1)==b'\0'
    return buzzes(m)
def tick(m,key=NEXT,gap=1000): return press(m,key,gap)+lift(m,key)
def debounced(m):
    """Stock's wheel lockout after a button runs out: on_wm_timer_setup_state counts it down."""
    for _ in range(8): assert m.call(address=syms['on_wm_timer_setup_state'],args=(0,0,0,0),gap=0,debounce=True)==8
    assert m.u.mem_read(KEY_LATCH,1)==b'\0'
LO,HI=80,140  # navigation.c OVERSHOOT_MIN_MS, OVERSHOOT_MAX_MS
PLAY_STATUS,OUTPUT_WAY=0xa3beac,0xa3beb0  # mclGetPlayStatus, mclGetOutputWay: their stock leaves read these
def routed(m,status=3,way=0,po=0,bal=0):
    """Key Tone's routing (ringnav_buzzer): the stock leaves run on these play status, output way and jacks."""
    for n in ('mclGetPlayStatus','mclGetOutputWay'): m.handlers.pop(syms[n],None)
    m.word(PLAY_STATUS,status); m.word(OUTPUT_WAY,way); m.byte(syms['g_po_status'],po); m.byte(syms['g_bal_status'],bal)
    return m
# Every Key Tone click (stock's three and ringnav()'s) goes through buzzeer_switch: stopped or paused
# with nothing plugged in, the buzzer as stock; while music plays, or with either jack plugged in or a
# Bluetooth (1) or USB (2) output, none; Key Tone off, none.
for kw,want in (({'status':1},1),({},1),({'status':2},0),({'po':1},0),({'bal':1},0),({'way':1},0),
                ({'way':2},0),({'status':2,'po':1},0)):
    m=routed(audible(Machine()),**kw); m.call(address=syms['buzzeer_switch'],args=(1,0,0,0))
    assert buzzes(m)==want,kw
m=routed(audible(Machine(),tone=0)); m.call(address=syms['buzzeer_switch'],args=(1,0,0,0)); assert not buzzes(m); passed()

if variant=='ipod':
    def long_list(n=40,m=None):
        m=m or Machine(); w,es=m.page_list(n,extent=n*48,name='allmusic_page'); m.paint(w); return m,w,es
    def turn(m,w,gaps,key=NEXT):
        got=[]
        for gap in gaps: assert m.call(key,gap=gap)==11; got.append(m.selected(w))
        return got
    # 1. After a stall the first tick moves, a second one LO to HI ms behind it does not, and the
    # third and later do. A second tick sooner than LO is a spin and one later than HI is a tick of
    # its own: neither is dropped. Short and long lists and virtual tables, from row 0.
    for table in (False,True):
        for count in (16,40):
            for gaps,want in (((1000,LO),[1,1]),((1000,HI),[1,1]),((1000,LO-1),[1,2]),((1000,HI+1),[1,2]),
                              ((1000,100,100),[1,1,2]),((1000,100,50),[1,1,2]),((1000,50,100),[1,2,3]),
                              ((1000,100,HI+1,100,100),[1,1,2,2,3]),((1000,1000,1000),[1,2,3])):
                m=Machine()
                if table:
                    w,rs,es=m.table_page(n=4,name='playerqueue_page',rebind=True); m.word(w+O['TABLE_ROWS'],count)
                else: w,es=m.page_list(count,extent=count*48)
                m.paint(w); assert turn(m,w,gaps)==want,(table,count,gaps)
                passed()
    # Home as built: the same, its ends are hard, and Centre opens the row that is highlighted.
    m=Machine(); view,imgs=home_list(m); click_target(m,imgs[2]); m.paint(view)
    assert turn(m,view,(1000,100,100,1000,100,100,100,100,1000))==[1,1,2,3,3,4,5,6,6]
    assert m.get(view+O['SCROLL_Y'])==0 and turn(m,view,(1000,100),PREV)==[5,5]
    assert m.confirm()==11 and m.dispatched()[0][1]==imgs[5]; passed()
    # A BUTTONS dialog: the same between its buttons, and nothing scrolls.
    m=Machine(); d=m.node('dialog','confirminfo_dialog'); m.word(d+O['W_PARENT'],m.wm); m.top=d
    buttons=[m.entry(d,220) for _ in range(3)]; m.nodes[d]['children']=buttons
    for b,x in zip(buttons,(20,147,274)): m.word(b+O['W_X'],x); m.word(b+O['W_W'],80); m.word(b+O['W_H'],80)
    m.paint(d); got=[]
    for gap in (1000,100,100): assert m.call(gap=gap)==11 and not m.moved(); got.append(m.selected(d))
    assert got==[1,1,2] and m.confirm()==11 and m.dispatched()[0][1]==buttons[2]; passed()

    # 2. The dropped tick is still an input: it is consumed, wakes the player's scrollbar and stops
    # momentum, but moves and sounds nothing, then or later, and leaves no timer. Centre then opens
    # the row shown.
    m=audible(Machine()); w,es=m.page_list(20,extent=960); bar=m.node('scroll_bar_m')
    parent=m.node('list_view',children=[w,bar]); m.word(w+O['W_PARENT'],parent); m.nodes[m.top]['children']=[parent]
    m.paint(w); assert tick(m)==1 and m.selected(w)==1
    m.word(w+O['VIEW_ANIMATOR'],0x1234)
    assert press(m,gap=100)==0 and lift(m)==0 and m.ret==11 and m.selected(w)==1 and not m.timers and not m.moved()
    assert [c[1] for c in m.calls if c[0]=='scroll_bar_scroll_to']==[bar] and m.get(w+O['VIEW_ANIMATOR'])==0
    m.advance(10000); assert m.selected(w)==1 and not buzzes(m)
    assert m.confirm()==11 and m.dispatched()[0][1]==es[1]; passed()

    # 3. A run belongs to its live list and direction. After each of these the next tick is the
    # first of a new run, so it moves although it comes LO ms after the last one, and the tick after
    # it is the one dropped. (Untouched, that next tick is the one dropped and the one after moves.)
    for change in ('none','window','pane','scope','context','count','recreated','gesture','animating','screen','unsupported',
                   'rejected','touch','click','missing','centre','return','play','long','reverse'):
        m=long_machine(); w,es=m.page_list(8,extent=8*48,name='allmusic_page'); m.paint(w)
        assert turn(m,w,(1000,1000))==[1,2]
        w=disturb(m,w,change,8)
        m.paint(w,gap=0); before=m.selected(w)
        assert m.call(gap=LO if change=='none' else LO-10)==11 and m.selected(w)==before+(change!='none'),change
        assert m.call(gap=LO)==11 and m.selected(w)==before+1,change
        passed()

    # 4. List ends. Reaching an end ends the run, and a turn against an end is never dropped: every
    # tick bumps and re-arms the stop, so ticks 100 or 200ms apart never wrap, and only a tick
    # EDGE_PAUSE_MS after the last one does, once. The tick after the wrap is a first one.
    def ring(name='playlist_page'):
        m=Machine(); w,es=m.page_list(6,height=96,extent=288,name=name); m.paint(w)
        assert turn(m,w,(1000,)*5)==[1,2,3,4,5]
        return m,w,es
    m,w,es=ring()
    for gap in (100,100,200,200,O['EDGE_PAUSE_MS']-1):
        assert m.call(gap=gap)==11 and m.selected(w)==5 and m.get(w+O['SCROLL_Y'])==192,gap
        m.paint(w,gap=0); assert m.sel()==(0,42,240,48)  # bumped against the end
    assert m.call(gap=O['EDGE_PAUSE_MS'])==11 and m.selected(w)==0 and m.get(w+O['SCROLL_Y'])==0
    m.paint(w,gap=0); assert m.sel()==(0,0,240,48)
    assert turn(m,w,(100,100,100))==[1,1,2] and turn(m,w,(100,100,100),PREV)==[1,1,0]
    assert m.call(PREV,gap=100)==11 and m.selected(w)==0
    m.paint(w,gap=0); assert m.sel()==(0,6,240,48)   # bumped down at the top, on the first tick
    assert m.call(PREV,gap=O['EDGE_PAUSE_MS'])==11 and m.selected(w)==5; passed()
    # Settings and Home keep hard ends: no bump and no wrap, however long the pause.
    m,w,es=ring('sysset_page')
    for gap in (100,O['EDGE_PAUSE_MS'],1000):
        assert m.call(gap=gap)==11 and m.selected(w)==5
        m.paint(w,gap=0); assert m.sel()==(0,48,240,48)
    passed()

    # 5. Key Tone follows the row. Each tick is the pair the window manager delivers: key-down-before
    # through the hook and the stock callback, then key-up-before. The count is the buzzer commands
    # the real buzzeer_switch issues. On Home as built, a slow tick, the dropped one after it and
    # two more leave rows 1, 1, 2, 3 and click 1, 0, 1, 1; stock's one-click-per-press latch is set by the press and cleared by the
    # release, as without the hook.
    def home_machine(tone=1):
        m=audible(Machine(),tone); view,imgs=home_list(m); click_target(m,imgs[2]); m.paint(view)
        return m,view
    m,view=home_machine(); got=[]
    for gap in (1000,100,100,1000):
        n=press(m,gap=gap); assert n==0 and m.down==0 and m.u.mem_read(SOUND_LATCH,1)==b'\x01'
        got.append((n+lift(m),m.selected(view))); assert m.ret==11
    assert got==[(1,1),(0,1),(1,2),(1,3)],got; passed()
    # Slow ticks click on every row; a turn against Home's hard end is silent; a repaint and a long
    # wait add nothing.
    m,view=home_machine()
    assert [tick(m) for _ in range(6)]==[1]*6 and m.selected(view)==6
    assert [tick(m,gap=gap) for gap in (1000,50,400)]==[0,0,0] and m.selected(view)==6
    m.paint(view); assert not buzzes(m)
    m.advance(10000); assert not buzzes(m); passed()
    # buzzeer_switch also runs when it is silent, so its calls are not the count: stock always calls
    # it on the press, with Key Tone held off; a row change adds the release's call, a dropped tick none.
    m,view=home_machine(); seen=[]
    m.u.hook_add(UC_HOOK_CODE,lambda u,a,size,x: seen.append(u.mem_read(TONE,1)[0]),begin=syms['buzzeer_switch'],end=syms['buzzeer_switch'])
    assert tick(m)==1 and seen==[0,1] and tick(m,gap=100)==0 and seen==[0,1,0]; passed()
    # An accelerated step is one click however many rows it takes (100ms ticks: the second is
    # dropped, the others step 1,1,2,2,3,3,4,4,5,5,6,6,7,7,8 rows).
    m,w,es=long_list(100,audible(Machine()))
    assert [tick(m,gap=100) for _ in range(16)]==[1,0]+[1]*14 and m.selected(w)==64; passed()
    # An end bump is silent and the wrap clicks once; the tick after it is a first one and clicks.
    m=audible(Machine()); w,es=m.page_list(6,height=96,extent=288,name='playlist_page'); m.paint(w)
    assert sum(tick(m) for _ in range(5))==5 and m.selected(w)==5
    assert [tick(m,gap=gap) for gap in (100,200,200,O['EDGE_PAUSE_MS']-1)]==[0,0,0,0] and m.selected(w)==5
    assert tick(m,gap=O['EDGE_PAUSE_MS'])==1 and m.selected(w)==0
    assert tick(m,gap=100)==1 and m.selected(w)==1; passed()
    # Rejected input is silent: a finger down, a window animation, stock's wheel lockout after a
    # button (until its countdown ends), and a turn while the screen is locked still clicks as stock.
    m,w,es=long_list(8,audible(Machine()))
    for field in ('pressed','animating'):
        setattr(m,field,1); assert tick(m)==0 and m.ret==11 and m.selected(w)==0; setattr(m,field,0)
    assert press(m,RETURN)==1 and lift(m,RETURN)==0 and m.ret==0 and m.u.mem_read(KEY_LATCH,1)==b'\x08'
    assert tick(m)==0 and m.ret==11 and m.selected(w)==0
    debounced(m); assert tick(m)==1 and m.selected(w)==1; passed()
    # Restoring the selection is not a step: a recreated page recalls its row on paint silently.
    m,w,es=long_list(20,audible(Machine()))
    assert [tick(m) for _ in range(6)]==[1]*6 and m.selected(w)==6
    w2,es2=m.page_list(20,extent=960,name='allmusic_page'); m.paint(w2)
    assert m.selected(w2)==6 and not buzzes(m); passed()
    # Centre is a button: its press clicks as stock, and the delayed confirmation adds nothing.
    m,w,es=long_list(8,audible(Machine()))
    assert press(m,CENTER)==1 and lift(m,CENTER)==0 and m.ret==11
    m.advance(200); assert m.clicks==[es[0]] and not buzzes(m); passed()

    # 6. The Key Tone setting. Off: everything above is silent and the rows still move. The click
    # uses the setting at the moment of the step, and the byte is never left changed: press() and
    # lift() check it after every call, whatever its value.
    m,view=home_machine(tone=0)
    assert [tick(m) for _ in range(4)]==[0,0,0,0] and m.selected(view)==4 and m.u.mem_read(TONE,1)==b'\0'; passed()
    m,view=home_machine()
    assert press(m)==0; m.byte(TONE,0); assert lift(m)==0 and m.selected(view)==1      # switched off in between
    assert press(m)==0; m.byte(TONE,1); assert lift(m)==1 and m.selected(view)==2      # and back on
    m.byte(TONE,0x5a); assert tick(m)==1 and m.u.mem_read(TONE,1)==b'\x5a'; passed()
    # The paths outside row navigation sound exactly as the stock binary does, press for press,
    # and do what they did: the volume, the carousels, the pixel-scroll fallback, the
    # buttons, a held key, a double press, and the wheel with the screen off or locked.
    def stock(keys,**flags):
        s=audible(Machine(patched=False))
        for flag,value in flags.items(): s.byte(syms[flag],value)
        out=[]
        for key in keys:
            out.append(press(s,key)); s.call(key,address=HOOKS['on_wm_keyup_before_fun'][0],gap=0,debounce=True)
        return out
    m=audible(Machine()); m.page('playing_page')
    assert [tick(m,key) for key in (NEXT,PREV,NEXT)]==stock((NEXT,PREV,NEXT))==[1,1,1] and m.ret==0; passed()
    m=audible(np_page())
    assert m.call(CENTER)==11 and m.call(CENTER,gap=100,clear=False)==11
    debounced(m)  # let the double press's stock wheel lockout run out
    assert [tick(m,gap=100) for _ in range(3)]==stock((NEXT,)*3)==[1,1,1] and m.nodes[m.slider]['value']==100; passed()
    m=audible(CoverflowMachine()); m.open()
    assert [tick(m,gap=20) for _ in range(2)]==[1,1] and slide(m,m.slide)[3]==120; passed()
    m=audible(Machine()); m.page('sysset_page','slide_menu')
    assert tick(m)==1 and [c[0] for c in m.moved()]==['slide_menu_scroll_to_next']; passed()
    m=audible(Machine()); w=m.page(t='table_client')
    assert [tick(m,gap=100) for _ in range(3)]==[1,1,1] and m.get(w+O['TABLE_TOP'])==288; passed()
    keys=(CENTER,RETURN,O['KEY_PLAY'],222,223)
    m,w,es=long_list(8,audible(Machine()))
    assert [tick(m,key) for key in keys]==stock(keys)==[1]*5 and m.selected(w)==0; passed()
    # Headphones in, or music playing: the buttons and a row change are silent.
    for kw in ({'po':1},{'way':1},{'status':2}):
        m,w,es=long_list(8,routed(audible(Machine()),**kw))
        assert tick(m)==0 and m.selected(w)==1 and [tick(m,key) for key in keys]==[0]*5
        assert m.u.mem_read(TONE,1)==b'\x01'
    passed()
    # A held key clicks once: stock's latch holds through its repeats and the long-press event.
    m,w,es=long_list(8,audible(long_machine()))
    assert [press(m,RETURN),press(m,RETURN,gap=500)]==[1,0]; long_return(m); assert not buzzes(m) and lift(m,RETURN)==0
    assert len(destinations(m))==0 and press(m,RETURN)==1; passed()
    m,w,es=long_list(3,audible(Machine()))
    assert press(m,CENTER)==1 and m.release()==11 and press(m,CENTER,gap=100)==1 and m.release()==0 and m.screens==[0]; passed()
    for flags in (dict(g_backlight_status=0),dict(g_backlight_status=0,g_keylock_flag=1,g_keylock_mode=1),
                  dict(g_lockscreen_pageflag=1),dict(g_testmode_flag=1)):
        m,w,es=long_list(8,audible(Machine()))
        for flag,value in flags.items(): m.byte(syms[flag],value)
        locked='g_keylock_flag' in flags
        assert [tick(m),tick(m,CENTER)]==stock((NEXT,CENTER),**flags)==[0 if locked else 1,1],flags
        assert m.selected(w)==0; passed()
    # Between a press and its release the page may change. List to volume: the silenced press is
    # spent, the volume step is stock's, and the next press there clicks. Volume to list: stock
    # already clicked, so the row change adds none.
    m,w,es=long_list(8,audible(Machine())); lst=m.top
    assert press(m)==0; m.page('playing_page'); assert lift(m)==0 and m.ret==0 and m.selected(w)==0
    assert tick(m)==1 and m.ret==0
    assert press(m)==1; m.top=lst; assert lift(m)==0 and m.ret==11 and m.selected(w)==1
    assert tick(m)==1 and m.selected(w)==2; passed()
    # A recreated page between the two, or a surface rebuilt at the same address: one click for the
    # one row change on the live list.
    m,w,es=long_list(8,audible(Machine()))
    assert press(m)==0; w2,es2=m.page_list(8,extent=8*48,name='allmusic_page'); m.paint(w2,gap=0)
    assert lift(m)==1 and m.selected(w2)==1 and m.selected(w)==0
    wiped(m,w2); m.paint(w2,gap=0); assert m.selected(w2)==1 and tick(m)==1 and m.selected(w2)==2; passed()
    # A dropped release (AWTK aborts pressed keys when windows change) leaves nothing behind. The
    # next pair on the list clicks once. A new press elsewhere is stock's again: stock's own latch,
    # still set by the dropped press, skips one click there, then the volume clicks as before. A
    # release without its press moves the row silently, and a button's press takes the ownership
    # back (stock's lockout then rejects that wheel release). Key Tone is never left off.
    m,w,es=long_list(8,audible(Machine())); lst=m.top
    assert press(m)==0 and tick(m)==1 and m.selected(w)==1
    assert press(m)==0; m.page('playing_page'); assert [tick(m),tick(m)]==[0,1] and m.ret==0
    m.top=lst; assert lift(m)==0 and m.ret==11 and m.selected(w)==2
    assert press(m)==0 and press(m,RETURN)==0 and lift(m)==0 and m.ret==11 and m.selected(w)==2
    assert lift(m,RETURN)==0 and press(m,RETURN)==1 and lift(m,RETURN)==0
    debounced(m); assert tick(m)==1 and m.selected(w)==3
    m.advance(10000); assert not buzzes(m) and m.u.mem_read(TONE,1)==b'\x01'; passed()
    CONFIG.clear()
else:
    # The Stock build has no key-down hook: the stock entry is untouched, every press clicks as the
    # stock binary does whether or not the row moves, the release adds nothing, every tick steps a
    # row and no setting is read.
    off=fileoff(demo,KEYDOWN); assert demo[off:off+12]==(B/'stock-demo').read_bytes()[off:off+12]
    m=audible(Machine()); w,es=m.page_list(3); m.paint(w)
    s=audible(Machine(patched=False)); got=[]
    for _ in range(4):
        got.append((press(m),lift(m),m.selected(w)))
        assert press(s)==got[-1][0]; s.call(address=HOOKS['on_wm_keyup_before_fun'][0],gap=0,debounce=True)
    assert got==[(1,0,1),(1,0,2),(1,0,2),(1,0,2)] and not m.config_reads; passed()

def fnv(s,h=2166136261):
    for c in s.encode(): h=((h^c)*16777619)&0xffffffff
    return (h*16777619)&0xffffffff

# Shuffle Songs and Most Played: after stock's 11 Local Music rows, two more in the same widgets and
# styles, moved first. Shuffle saves shuffle as the play-mode setting does and folder-plays every song
# from a random track, leaving the staging deque as it was; an empty library only says so. Most
# Played folder-plays the counted songs, most played first, and leaves the play mode alone.
class ShuffleMachine(CoverflowMachine):
    def __init__(self):
        super().__init__()
        self.counts=b''; self.queued=None; self.wifi=-1; self.card=[]  # /mnt/mmc's (name, d_type) entries
        for n in ('getAllMusic','toolsRandnum','widget_restack','fread@GLIBC_2.0','fclose@GLIBC_2.2','get_wifisignal',
                  'opendir@GLIBC_2.0','readdir@GLIBC_2.0','closedir@GLIBC_2.0','navigator_to'): self.handlers[syms[n]]='s:'+n
        self.mock('pthread_mutex_lock@GLIBC_2.0','pthread_mutex_unlock@GLIBC_2.0')
        self.handlers[syms['fopen@GLIBC_2.2']]='s:fopen'
        self.handlers[int(manifest['patch_symbols']['stock_localmusic_trampoline'],16)]='stock_localmusic'
    def hook(self,u,address,size,unused):
        name=self.handlers.get(address,'')
        if address==syms['navigator_to_with_context'] and self.text(u.reg_read(REGS[0]))=='playing_page':
            self.queued=self.names(self.get(u.reg_read(REGS[1])))
        if not name.startswith('s:'): return super().hook(u,address,size,unused)
        name=name[2:].split('@')[0]; a,b=u.reg_read(REGS[0]),u.reg_read(REGS[1]); ret=0; self.calls.append((name,a,b))
        if name=='fopen': ret=1 if self.text(a)=='/mnt/data/ringnav-plays' and self.counts else 0
        elif name=='fread': self.u.mem_write(a,self.counts); ret=1
        elif name=='getAllMusic':
            self.deqs[self.get(syms['tools_pdeq_directory'])][1]=[self.copy('stSongInfo',e) for e in self.found]; ret=len(self.found)
        elif name=='toolsRandnum': ret=a-1  # stock: rand() % a
        elif name=='get_wifisignal': ret=self.wifi
        elif name=='opendir': ret=0x3000000 if self.text(a)=='/mnt/mmc' else 0; self.listed=list(self.card)
        elif name=='readdir' and self.listed:
            n,t=self.listed.pop(0); ret=self.alloc(268); self.byte(ret+10,t); self.u.mem_write(ret+11,n.encode()+b'\0')
        elif name=='widget_restack':
            kids=next(n['children'] for n in self.nodes.values() if a in n['children']); kids.remove(a); kids.insert(b,a)
        for r in [UC_MIPS_REG_V1,*REGS,UC_MIPS_REG_T8,UC_MIPS_REG_T9]: u.reg_write(r,0xdeadbeef)
        u.reg_write(UC_MIPS_REG_V0,ret&0xffffffff); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))
m=ShuffleMachine(); stock=[m.node('list_item') for _ in range(11)]
view=m.node('scroll_view','scroll_view_localmusic',stock); m.top=m.node('window','localmusic_page',[view])
assert m.call(address=HOOKS['localmusic_page_init'][0],args=(m.top,5,0,0),gap=0)==0
assert m.calls[0][:3]==('stock_localmusic',m.top,5)
# Library order: Shuffle Songs; Artist, Album, All Songs, Genre, Playlist, My Fav; Recently Added, Recent,
# Most Played, Frequent, Hi-Res; then Update Local Music, last (stock rows by get_localmusic_showinfo index).
kids=m.nodes[view]['children']; row,top=kids[0],kids[9]
assert [kids[i] for i in (*range(1,9),10,11,12)]==[stock[i] for i in (3,2,1,4,10,6,9,8,7,5,0)] and m.nodes[row]['style']=='s_listitem_black'
icon,label=m.nodes[m.nodes[top]['children'][0]]['children']
assert m.nodes[icon]['image']=='local_frequentplay' and m.nodes[label]['text']=='Most Played'
button=m.nodes[row]['children'][0]; icon,label=m.nodes[button]['children']
assert m.nodes[button]['style']=='s_btn_listitem' and [m.get(button+O[k]) for k in ('W_X','W_Y','W_W','W_H')]==[20,0,335,70]
assert m.nodes[icon]['image']=='local_shuffle' and [m.get(icon+O[k]) for k in ('W_X','W_Y','W_W','W_H')]==[10,0,52,70]
assert m.nodes[label]['style']=='s_scrlabel_white24l' and m.nodes[label]['text']=='Shuffle Songs' and not m.nodes[button].get('name')
f,ctx=m.handler(button,O['EVT_CLICK'])
assert m.call(address=f,args=(ctx,m.event,0,0),gap=0)==0
def called(n): return [c[1:3] for c in m.calls if c[0]==n]
assert called('config_playmode')==[(2,1)] and [a for a,_ in called('toolsRandnum')]==[2] and m.plays==[('playing_page',m.plays[0][1],1,1,2)]
assert m.names(m.get(syms['tools_pdeq_directory']))==['staged'] and not m.toasts; passed()
m.found=[]; m.plays=[]; m.calls=[]
assert m.call(address=f,args=(ctx,m.event,0,0),gap=0)==0
assert not called('config_playmode') and not m.plays and m.toasts[-1][0]=='dialog/msginfo_dialog' and m.toasts[-1][3]=='Update Local Music first'; passed()
# Most Played opens a black mostplayed_page list (nothing played: "No plays yet", no rows); a second
# press while it is open does nothing. Its rows are the top PLAYS_TOP, most played first and, among
# equal counts, the most recently counted (earlier slot) first; a row folder-plays that ranked list
# from itself, leaving the play mode alone.
f,ctx=m.handler(m.nodes[top]['children'][0],O['EVT_CLICK'])
m.found=[m.song(n) for n in ('T1','T2','T3')]; m.calls=[]; toasts=len(m.toasts)
assert m.call(address=f,args=(ctx,m.event,0,0),gap=0)==0 and not m.plays and len(m.toasts)==toasts
m.page=m.top; assert m.nodes[m.page]['name']=='mostplayed_page' and m.nodes[m.page]['style:normal:bg_color']==-0x1000000
assert m.texts()==['No plays yet'] and not m.nodes[m.find('scroll_view')]['children']
assert m.call(address=f,args=(ctx,m.event,0,0),gap=0)==0 and m.top==m.page; m.close(); passed()
m=ShuffleMachine(); m.found=[m.song(f'T{i}') for i in range(130)]
m.word(m.found[1]+O['REC_ARTIST'],0)  # untagged: the count alone
slots=[(fnv('/p/T3'),5),(fnv('/p/T1'),2),(fnv('/p/T7'),5)]+[(fnv(f'/p/T{i}'),1) for i in range(8,130)]
m.counts=b''.join(struct.pack('<2I',*e) for e in slots).ljust(8*O['PLAYS_SLOTS'],b'\0')
assert m.call(address=f,args=(ctx,m.event,0,0),gap=0,count=50_000_000)==0; m.page=m.top
ranked=['T3','T7','T1']+[f'T{i}' for i in range(8,130)][:O['PLAYS_TOP']-3]
# Each 64px row: the title over its artist (when tagged) and play count, in 16px stock grey.
details=['Artist · 5 plays','Artist · 5 plays','2 plays']+['Artist · 1 play']*(O['PLAYS_TOP']-3)
assert m.texts()==['Most Played']+[x for p in zip(ranked,details) for x in p]
rows=m.nodes[m.find('scroll_view')]['children']; assert len(rows)==O['PLAYS_TOP']
assert [(m.get(rows[1]+O['W_Y']),m.get(rows[1]+O['W_H']))]==[(64,64)]
title,detail=m.nodes[rows[1]]['children']
assert m.nodes[title]['style']=='s_scrlabel_white20c' and m.get(title+O['W_Y'])+m.get(title+O['W_H'])<=m.get(detail+O['W_Y'])
assert m.nodes[detail]['style:normal:text_color']==-0x555556 and m.nodes[detail]['style:normal:font_size']==16
assert m.get(detail+O['W_Y'])+m.get(detail+O['W_H'])<=64
r=m.nodes[m.find('scroll_view')]['children'][2]; g,c=m.handler(r,O['EVT_CLICK'])
assert m.call(address=g,args=(c,m.event,0,0),gap=0)==0
assert m.queued==ranked and m.plays==[('playing_page',m.plays[0][1],2,1,2)] and not called('config_playmode'); passed()
# A Centre hold on a row opens the song menu, as Coverflow's tracks do, over the ranked list.
m.handlers[syms['navigator_to']]='q:navigator_to'; view=m.find('scroll_view'); m.paint(view); m.call()
m.press(100); assert m.hold()==11 and m.nodes[m.title]['text']=='T7' and m.release()==0
assert m.labels()==['Play next','Add to queue','Add to Favourites','Go to artist']; passed()
# Opened again, the kept tracks are ranked again without reading the library; a library change reads it again.
m.close(); m.found=m.found[:8]; m.calls=[]
assert m.call(address=f,args=(ctx,m.event,0,0),gap=0,count=50_000_000)==0 and not called('getAllMusic')
m.page=m.top; assert m.texts()==['Most Played']+[x for p in zip(ranked,details) for x in p]
m.close(); m.calls=[]; m.rescan(); m.calls=[]
assert m.call(address=f,args=(ctx,m.event,0,0),gap=0,count=50_000_000)==0 and called('getAllMusic')
m.page=m.top; assert m.texts()==['Most Played']+[x for p in zip(ranked[:3],details[:3]) for x in p]; passed()

# Upload Scrobbles: the second-last row, above Update Local Music, only with an account in the card's .scrobble.ini (a
# ListenBrainz token or all four Last.fm keys). Without Wi-Fi it only says so; otherwise scrobble.c's thread
# starts, a second press finds it running, and a timer reports the result once the thread ends.
m=ShuffleMachine(); m.config.update(USER='u',PASSWORD='p',API_KEY='k')  # no API_SECRET: no row
view=m.node('scroll_view','scroll_view_localmusic',[m.node('list_item') for _ in range(11)]); m.top=m.node('window','localmusic_page',[view])
assert m.call(address=HOOKS['localmusic_page_init'][0],args=(m.top,5,0,0),gap=0)==0 and len(m.nodes[view]['children'])==13
assert ('/mnt/mmc/.scrobble.ini','LASTFM','API_SECRET','') in m.config_reads
m.config['TOKEN']='tok'; m.nodes[view]['children']=[m.node('list_item') for _ in range(11)]
assert m.call(address=HOOKS['localmusic_page_init'][0],args=(m.top,5,0,0),gap=0)==0 and len(m.nodes[view]['children'])==14
button=m.nodes[m.nodes[view]['children'][-2]]['children'][0]; icon,label=m.nodes[button]['children']
assert m.nodes[icon]['image']=='local_scrobble' and m.nodes[label]['text']=='Upload Scrobbles' and not m.nodes[button].get('name')
f,ctx=m.handler(button,O['EVT_CLICK'])
assert m.call(address=f,args=(ctx,m.event,0,0),gap=0)==0 and m.toasts[-1][3]=='Connect to Wi-Fi first' and not m.threads
m.wifi=3; assert m.call(address=f,args=(ctx,m.event,0,0),gap=0)==0 and m.toasts[-1][3]=='Uploading scrobbles' and len(m.threads)==1
assert m.call(address=f,args=(ctx,m.event,0,0),gap=0)==0 and m.toasts[-1][3]=='Already uploading' and len(m.threads)==1
m.advance(2000); assert m.toasts[-1][3]=='Already uploading' and not m.joins  # still uploading
worker,arg=m.threads[0]; assert m.call(address=worker,args=(arg,0,0,0),gap=0)==0  # no log on the card
m.advance(600); assert m.toasts[-1][3]=='Nothing to upload' and m.joins==[77] and not m.timers; passed()

# Podcasts and Audiobooks: a row each, above upkeep, only for the card's top-level folder of that name (case
# aside, a directory). A press opens folder_page there as a deeper folder (layer 3, stock reload, title
# and rebuild); Back there leaves the page (layer 1 before stock's step down), and only for that root.
m=ShuffleMachine(); m.card=[('Music',4),('PODCASTS',4),('Audiobooks',8)]
m.handlers[int(manifest['patch_symbols']['stock_folder_trampoline'],16)]='stock_folder'
m.handlers[int(manifest['patch_symbols']['stock_folder_back_trampoline'],16)]='stock_folder_back'
view=m.node('scroll_view','scroll_view_localmusic',[m.node('list_item') for _ in range(11)]); m.top=m.node('window','localmusic_page',[view])
assert m.call(address=HOOKS['localmusic_page_init'][0],args=(m.top,5,0,0),gap=0)==0 and len(m.nodes[view]['children'])==14
button=m.nodes[m.nodes[view]['children'][-2]]['children'][0]; icon,label=m.nodes[button]['children']
assert m.nodes[icon]['image']=='local_podcasts' and m.nodes[label]['text']=='Podcasts' and not m.nodes[button].get('name')
f,ctx=m.handler(button,O['EVT_CLICK']); m.calls=[]
assert m.call(address=f,args=(ctx,m.event,0,0),gap=0)==0 and [m.text(c[1]) for c in m.calls if c[0]=='navigator_to']==['folder_page']
win=m.node('window','folder_page'); m.calls=[]
assert m.call(address=HOOKS['folder_page_init'][0],args=(win,0,0,0),gap=0)==0
assert m.text(syms['g_folder_path'])=='/mnt/mmc/PODCASTS' and m.u.mem_read(syms['g_folder_layer'],1)[0]==3
assert [c[0] for c in m.calls]==['stock_folder','memcpy','folder_reload_data','folder_reinit_navbarname','folder_refresh'] and m.calls[-1][1]==win
assert m.call(address=HOOKS['folder_back'][0],args=(0,0,0,0),gap=0)==0 and m.calls[-1][0]=='stock_folder_back'
assert m.u.mem_read(syms['g_folder_layer'],1)[0]==1; passed()
m.byte(syms['g_folder_layer'],3); assert m.call(address=HOOKS['folder_page_init'][0],args=(win,0,0,0),gap=0)==0  # the Folder view
m.byte(syms['g_folder_layer'],3); m.call(address=HOOKS['folder_back'][0],args=(0,0,0,0),gap=0)
assert m.u.mem_read(syms['g_folder_layer'],1)[0]==3; passed()

# Photos (patch/photos.c): the helpers first. Names: .jpg, .jpeg and .png, case aside, not hidden.
m=Machine(); m.mock('memcmp@GLIBC_2.0',prefix='cmp:')
def ph_call(name,*args): return m.call(address=ps[name],args=(*args,0,0,0,0)[:4],gap=0,count=5_000_000)
assert [ph_call('photo_file',m.string(n)) for n in ('a.JPG','b.jpeg','c.Png','d.gif','._e.jpg','.f.jpg','g','h.jpg.txt')]==[1,1,1,0,0,0,0,0]
passed()
# EXIF orientation: either byte order, after another segment, within the bytes read; else 1.
def exif(o,le=True,pad=b''):
    e='<' if le else '>'
    tiff=(b'II' if le else b'MM')+struct.pack(e+'HI',42,8)+struct.pack(e+'H',2)+struct.pack(e+'HHI',0x10f,2,1)+bytes(4)
    tiff+=struct.pack(e+'HHIH',0x112,3,1,o)+bytes(2)+bytes(4)
    app1=b'Exif\0\0'+tiff
    return b'\xff\xd8'+pad+b'\xff\xe1'+struct.pack('>H',len(app1)+2)+app1+b'\xff\xda'
JFIF=b'\xff\xe0'+struct.pack('>H',16)+b'JFIF\0'+bytes(9)
def orientation(data): a=m.alloc(len(data)+4); m.u.mem_write(a,data); return ph_call('photo_orientation',a,len(data))
assert [orientation(d) for d in (exif(6),exif(8,le=False),exif(3,pad=JFIF),exif(9),exif(6)[:30],b'\x89PNG\r\n\x1a\n',b'\xff\xd8\xff\xda')]==[6,8,3,1,1,1,1]
passed()
# Orientation: each of the eight against its EXIF meaning (PIL's exif_transpose), the channels from
# the format's byte order, alpha over black, and a crop to the box.
W,H=3,2
def stored(x,y): return (10*x+y+1,100+x,200+y,255)
def shown(o,x,y):  # the stored pixel shown at x, y
    return {1:(x,y),2:(W-1-x,y),3:(W-1-x,H-1-y),4:(x,H-1-y),5:(y,x),6:(y,H-1-x),7:(W-1-y,H-1-x),8:(W-1-y,x)}[o]
src=m.alloc(64); at=m.alloc(4); dst=m.alloc(256); size=m.alloc(8)
for o in range(1,9):
    for y in range(H):
        for x in range(W): r,g,b,a=stored(x,y); m.u.mem_write(src+y*16+x*4,bytes([b,g,r,a]))  # BGRA, format 3
    m.u.mem_write(at,bytes([2,1,0,3]))
    m.call(address=ps['photo_orient'],args=(src,W,H,16),stack=(at,o,dst,4,size),gap=0,count=5_000_000)
    sw,sh=(H,W) if o>=5 else (W,H)
    assert (m.get(size),m.get(size+4))==(sw,sh),o
    for y in range(sh):
        for x in range(sw):
            r,g,b,_=stored(*shown(o,x,y)); assert m.get(dst+(y*4+x)*4)==0xff000000|b<<16|g<<8|r,(o,x,y)
m.u.mem_write(src,bytes([200,100,50,128]))  # half transparent, over black
m.call(address=ps['photo_orient'],args=(src,W,H,16),stack=(at,1,dst,2,size),gap=0,count=5_000_000)
assert (m.get(size),m.get(size+4))==(2,2) and m.get(dst)==0xff000000|(200*128//255)<<16|(100*128//255)<<8|50*128//255
passed()
# Placing: fitted into the box, centred, never enlarged.
r=m.alloc(16)
for (w,h,bw,bh),want in (((100,50,85,72),(0,15,85,42)),((40,30,85,72),(22,21,40,30)),((50,100,85,72),(24,0,36,72)),
                         ((281,375,375,290),(79,0,217,290))):
    m.call(address=ps['photo_place'],args=(w,h,0,0),stack=(bw,bh,r),gap=0); assert tuple(signed(m.get(r+4*i)) for i in range(4))==want,(w,h,want)
passed()

class PhotosMachine(DepthMachine):
    """The card as a tree of {path: [(name, d_type)]}, photo bytes by path, and the cache's files."""
    def __init__(self,tree,data):
        super().__init__()
        self.tree=tree; self.data=data; self.cache=set(); self.thumbs=[]; self.bad=set(); self.open_files={}; self.dirs={}
        for n in ('opendir@GLIBC_2.0','readdir@GLIBC_2.0','closedir@GLIBC_2.0','qsort@GLIBC_2.0','fopen@GLIBC_2.2',
                  'fread@GLIBC_2.0','fclose@GLIBC_2.2','access@GLIBC_2.0','rename@GLIBC_2.0','unlink@GLIBC_2.0',
                  'memcmp@GLIBC_2.0','toolsThumbSpecCover','pthread_mutex_lock@GLIBC_2.0','pthread_mutex_unlock@GLIBC_2.0'):
            self.handlers[syms[n]]='p:'+n
        tramp=int(manifest['patch_symbols']['stock_localmusic_trampoline'],16)
        self.handlers[tramp]='stock_localmusic'; self.u.hook_add(UC_HOOK_CODE,self.hook,begin=tramp,end=tramp)  # fast() ran
    def hook(self,u,address,size,unused):
        name=self.handlers.get(address,'')
        a,b,c,d=[u.reg_read(r) for r in REGS]
        if name=='c:calloc@GLIBC_2.0' and a*b<=0x10000: ret=self.alloc((a*b+7)&~3)  # word-aligned, unlike Coverflow's mock
        elif name=='widget_load_image' and '/mnt/mmc/.photos/' in self.text(b):
            url=self.text(b); self.loads.append(url)
            if url[7:] not in self.cache: ret=1
            else:  # thumbnails decode 72x54, screen copies 375x281 (a turned photo's, as stored)
                w,h=(72,54) if url.endswith('t.jpg') else (375,281); px=self.big_alloc(w*h*4)
                self.u.mem_write(px,bytes([90,120,150,255])*(w*h))
                self.word(c,w); self.word(c+4,h); self.word(c+8,w*4); self.u.mem_write(c+0xc,struct.pack('<HH',0,1)); self.word(c+0x14,px); ret=0
        elif name=='canvas_draw_image':
            self.draws.append(tuple(tuple(signed(self.get(r+4*i)) for i in range(4)) for r in (c,d))); ret=0
        elif name.startswith('p:'):
            name=name[2:].split('@')[0]; ret=0; text=self.text(a) if name not in ('readdir','closedir','qsort','fread','fclose','memcmp') else ''
            if name=='opendir':
                ret=0 if text not in self.tree else self.alloc(4); self.dirs[ret]=list(self.tree.get(text,[]))
            elif name=='readdir' and self.dirs.get(a):
                n,t=self.dirs[a].pop(0); ret=self.alloc(268); self.byte(ret+10,t); self.u.mem_write(ret+11,n.encode()+b'\0')
            elif name=='qsort':
                ptrs=[self.get(a+4*i) for i in range(b)]
                for i,p in enumerate(sorted(ptrs,key=self.text)): self.word(a+4*i,p)
            elif name=='fopen':
                if text in self.data: ret=self.alloc(4); self.open_files[ret]=self.data[text]
                elif text.endswith('.bad'): self.bad.add(text); ret=self.alloc(4)
            elif name=='fread': chunk=self.open_files.get(d,b'')[:b*c]; self.u.mem_write(a,chunk); ret=len(chunk)//b
            elif name=='access': ret=0 if text in self.cache or text in self.bad or text in self.data else -1
            elif name=='toolsThumbSpecCover':
                self.thumbs.append((self.text(a),self.text(b),c,d))
                ret=0 if self.data.get(self.text(a))==b'corrupt' else 1
                if ret: self.cache.add(self.text(b))
            elif name=='rename': self.cache.discard(text); self.cache.add(self.text(b))
            elif name=='unlink': self.cache.discard(text)
            elif name=='memcmp': ret=0 if bytes(self.u.mem_read(a,c))==bytes(self.u.mem_read(b,c)) else 1
        else: return super().hook(u,address,size,unused)
        self.calls.append((name,a,b,c))
        for r in [UC_MIPS_REG_V1,*REGS,UC_MIPS_REG_T8,UC_MIPS_REG_T9]: u.reg_write(r,0xdeadbeef)
        u.reg_write(UC_MIPS_REG_V0,ret&0xffffffff); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))

# Local Music's last row opens the card's Photos folder (case aside) as albums: All Photos, then
# each subfolder holding a photo, by name; hidden files, other types and empty folders are left out.
R='/mnt/mmc/photos'
tree={'/mnt/mmc':[('Music',4),('photos',4)],R:[('b.jpg',8),('A.png',8),('._b.jpg',8),('notes.txt',8),('Trip',4),('Empty',4),('.hidden',4)],
      R+'/Trip':[('2.JPG',8),('1.jpg',0)],R+'/Empty':[('x.txt',8)],R+'/.hidden':[('h.jpg',8)]}
data={R+'/b.jpg':exif(6),R+'/A.png':b'\x89PNG',R+'/Trip/1.jpg':b'corrupt',R+'/Trip/2.JPG':exif(1)}
m=PhotosMachine(tree,data)
view=m.node('scroll_view','scroll_view_localmusic',[m.node('list_item') for _ in range(11)]); m.top=m.node('window','localmusic_page',[view])
assert m.call(address=HOOKS['localmusic_page_init'][0],args=(m.top,5,0,0),gap=0,count=5_000_000)==0
button=m.nodes[m.nodes[view]['children'][-1]]['children'][0]; icon,label=m.nodes[button]['children']
assert m.nodes[icon]['image']=='local_photos' and m.nodes[label]['text']=='Photos' and len(m.nodes[view]['children'])==14
f,ctx=m.handler(button,O['EVT_CLICK']); assert m.call(address=f,args=(ctx,m.event,0,0),gap=0,count=5_000_000)==0
page=m.top; assert m.nodes[page]['name']=='photos_page' and m.nodes[page]['style:normal:bg_color']==-0x1000000
albums,grid,viewer=m.nodes[page]['children']; assert not m.nodes[grid]['visible'] and not m.nodes[viewer]['visible']
def labels(w): return [m.nodes[x].get('text') for x in m.nodes if m.nodes[x]['type']=='hscroll_label' and m.get(x+O['W_PARENT']) in m.nodes and x in m.nodes[m.get(x+O['W_PARENT'])]['children'] and under(x,w)]
def under(x,w):
    while x and x!=w: x=m.get(x+O['W_PARENT']) if m.get(x+O['W_PARENT']) in m.nodes else 0
    return x==w
assert labels(albums)==['Photos','All Photos','Trip']; passed()
# All Photos: the folder's own, then Trip's, by name; four tiles of three whole rows' grid, each a
# s_listitem_black list_item in a bare vertical scroll view, and the worker for the uncached ones.
rows=m.find('scroll_view',albums); first=m.nodes[rows]['children'][0]
f,ctx=m.handler(first,O['EVT_CLICK']); m.call(address=f,args=(ctx,m.event,0,0),gap=0); m.advance(0)
assert not m.nodes[albums]['visible'] and m.nodes[grid]['visible'] and labels(grid)==['All Photos']
tiles_view=m.find('scroll_view',grid); tiles=m.nodes[tiles_view]['children']
assert m.nodes[tiles_view].get('yslidable')==1 and m.nodes[tiles_view].get('virtual_h')==O['PH_TILE_H']
assert [cf_geometry(m,t) for t in tiles]==[(O['PH_GRID_X']+i*O['PH_TILE_W'],0,O['PH_TILE_W'],O['PH_TILE_H']) for i in range(4)]
assert all(m.nodes[t]['style']=='s_listitem_black' for t in tiles) and len(m.threads)==1
paths=[R+'/A.png',R+'/b.jpg',R+'/Trip/1.jpg',R+'/Trip/2.JPG']; keys=[fnv(p) for p in paths]
def cache(i,s): return f'/mnt/mmc/.photos/{keys[i]:08x}{s}'
# Grey until made; the worker makes each photo's screen copy (sides swapped for a turned photo),
# then its thumbnail from that copy, and marks the corrupt one bad.
def border(w): return m.call(address=HOOKS['widget_on_paint_border'][0],args=(w,m.canvas,0,0),gap=0,clear=False,count=50_000_000)
border(tiles[0]); assert m.bands[-1][:5]==((O['PH_TILE_W']-O['PH_THUMB_W'])//2,(O['PH_TILE_H']-O['PH_THUMB_H'])//2,O['PH_THUMB_W'],O['PH_THUMB_H'],O['PH_GREY'])
worker,arg=m.threads[0]; assert m.call(address=worker,args=(arg,0,0,0),gap=0,count=20_000_000)==0
SW,SH,TW,TH=O['PH_SHOT_W'],O['PH_SHOT_H'],O['PH_THUMB_W'],O['PH_THUMB_H']
assert m.thumbs==[(paths[0],cache(0,'.tmp'),SW,SH),(cache(0,'.jpg'),cache(0,'.tmp'),TW,TH),
                  (paths[1],cache(1,'.tmp'),SH,SW),(cache(1,'.jpg'),cache(1,'.tmp'),TH,TW),
                  (paths[2],cache(2,'.tmp'),SW,SH),
                  (paths[3],cache(3,'.tmp'),SW,SH),(cache(3,'.jpg'),cache(3,'.tmp'),TW,TH)],m.thumbs
assert m.bad=={cache(2,'.bad')} and cache(2,'.jpg') not in m.cache
m.advance(250); assert m.joins==[77]; passed()
# The turned photo's thumbnail is drawn upright, centred in its tile; the bad one stays grey.
border(tiles[1]); assert m.draws[-1]==((0,0,54,72),((O['PH_TILE_W']-54)//2,(O['PH_TILE_H']-O['PH_THUMB_H'])//2,54,72))
border(tiles[2]); assert m.bands[-1][4]==O['PH_GREY']
# Centre on a tile: the viewer, a slide_menu one photo per child, a screen wide per step, drawing
# the photo fitted and centred; at rest its neighbours are decoded too.
f,ctx=m.handler(tiles[1],O['EVT_CLICK']); m.call(address=f,args=(ctx,m.event,0,0),gap=0); m.advance(0)
s=m.find('slide_menu',viewer); m.slide=s
assert m.nodes[viewer]['visible'] and not m.nodes[grid]['visible'] and len(m.nodes[s]['children'])==4
assert signed(m.get(s+O['SLIDE_SPACER']))+O['PH_SHOT_H']==375 and m.get(s+O['SLIDE_INDEX'])==1
m.draws=[]; m.loads=[]; paint(m)
assert m.draws==[((0,0,281,375),((375-217)//2,0,217,290))] and m.loads==['file://'+cache(1,'.jpg'),'file://'+cache(0,'.jpg')]
# Mid-slide back to the first photo: this one moves right and the first follows a screen behind it.
m.draws=[]; m.word(s+O['SLIDE_OFFSET'],375//4); paint(m)
(_,a),(_,b)=m.draws; assert len(m.draws)==2 and a[0]-(375-217)//2==b[0]+375 and 0<a[0]-(375-217)//2<375//2 and b[1:]==(4,375,281)
m.word(s+O['SLIDE_OFFSET'],0); passed()
# Centre shows "2 of 4" and the name; a bad photo says so whatever.
info=[x for x in m.nodes[viewer]['children'] if m.nodes[x]['type']=='hscroll_label'][0]
x,y,bw,bh=cf_geometry(m,info); inset=max(corner_inset(30+y),corner_inset(30+y+bh))
assert not m.nodes[info]['visible'] and inset<=x and x+bw<=375-inset  # the whole rounded band clears the glass
f,ctx=m.handler(m.nodes[s]['children'][1],O['EVT_CLICK']); m.call(address=f,args=(ctx,m.event,0,0),gap=0)
assert m.nodes[info]['visible'] and m.nodes[info]['text']=='2 of 4  b.jpg' and m.nodes[info]['style:normal:bg_color']==signed(0xb3000000)
m.call(address=f,args=(ctx,m.event,0,0),gap=0); assert not m.nodes[info]['visible']
m.word(s+O['SLIDE_INDEX'],2); f,ctx=m.handler(s,O['EVT_VALUE_CHANGED']); m.call(address=f,args=(ctx,m.event,0,0),gap=0)
assert m.nodes[info]['visible'] and m.nodes[info]['text']=="Can't open this photo"; passed()
# The wheel steps the viewer a screen at a time, as Coverflow's covers.
m.top=page; assert m.call()==11 and signed(m.get(m.get(s+O['SLIDE_ANIMATOR'])+O['ANIM_X_TO']))==-375; passed()
# Return: the grid with the photo last shown selected, then the albums, then Local Music.
m.page=page; assert m.key()==11 and m.nodes[grid]['visible'] and not m.nodes[viewer]['visible']
assert m.nodes[tiles_view]['_ringnav_index']==2 and m.nodes[tiles_view]['_ringnav_count']==4
assert m.key()==11 and m.nodes[albums]['visible'] and not m.nodes[grid]['visible']
assert m.key()==11 and [c[0] for c in m.calls].count('navigator_back')==1
m.close(); assert sorted(m.destroyed)==sorted(m.frames) and m.frames; passed()

class BooksMachine(DepthMachine):
    """The card as a tree of {path: [(name, d_type)]} and files as {path: bytearray}, with stdio
    handles over them, and a fixed-pitch font: every character BOOK_PITCH wide."""
    def __init__(self,tree,files):
        super().__init__()
        self.tree=tree; self.files=files; self.handles={}; self.dirs={}; self.lines=[]
        for n in ('opendir@GLIBC_2.0','readdir@GLIBC_2.0','closedir@GLIBC_2.0','qsort@GLIBC_2.0','fopen@GLIBC_2.2',
                  'fread@GLIBC_2.0','fwrite@GLIBC_2.0','fseek@GLIBC_2.0','ftell@GLIBC_2.0','fclose@GLIBC_2.2',
                  'access@GLIBC_2.0','rename@GLIBC_2.0','unlink@GLIBC_2.0','mkdir@GLIBC_2.0','memcmp@GLIBC_2.0',
                  'strlen@GLIBC_2.0','strstr@GLIBC_2.0','canvas_measure_text','canvas_draw_text',
                  'fork@GLIBC_2.0','execl@GLIBC_2.0','exit@GLIBC_2.0','waitpid@GLIBC_2.0','socket@GLIBC_2.0',
                  'sendto@GLIBC_2.0','close@GLIBC_2.0'):
            self.handlers[syms[n]]='b:'+n
        self.handlers[syms['mclGetOutputWay']]='b:mclGetOutputWay'
        self.forked=4242; self.waited=0; self.way=0; self.execs=[]; self.sent=[]
        tramp=int(manifest['patch_symbols']['stock_localmusic_trampoline'],16)
        self.handlers[tramp]='stock_localmusic'; self.u.hook_add(UC_HOOK_CODE,self.hook,begin=tramp,end=tramp)
    def hook(self,u,address,size,unused):
        name=self.handlers.get(address,'')
        a,b,c,d=[u.reg_read(r) for r in REGS]; ret=0
        if name=='c:calloc@GLIBC_2.0': ret=self.alloc((a*b+7)&~3) if a*b<=0x10000 else self.big_alloc(a*b)  # word-aligned
        elif not name.startswith('b:'): return super().hook(u,address,size,unused)
        name=name[2:].split('@')[0]
        if name=='opendir':
            path=self.text(a); ret=self.alloc(4) if path in self.tree else 0; self.dirs[ret]=list(self.tree.get(path,[]))
        elif name=='readdir' and self.dirs.get(a):
            n,t=self.dirs[a].pop(0); ret=self.alloc(268); self.byte(ret+10,t); self.u.mem_write(ret+11,n.encode()+b'\0')
        elif name=='qsort':
            ptrs=[self.get(a+4*i) for i in range(b)]
            for i,p in enumerate(sorted(ptrs,key=self.text)): self.word(a+4*i,p)
        elif name=='fopen':
            path,mode=self.text(a),self.text(b)
            if 'w' in mode: self.files[path]=bytearray()
            if path in self.files: ret=self.alloc(4); self.handles[ret]=[path,0]
        elif name in ('fread','fwrite'):
            path,pos=self.handles[d]; f=self.files[path]
            if name=='fread': chunk=bytes(f[pos:pos+b*c]); self.u.mem_write(a,chunk)
            else: chunk=bytes(u.mem_read(a,b*c)); f[pos:pos+len(chunk)]=chunk
            self.handles[d][1]+=len(chunk); ret=len(chunk)//b
        elif name=='fseek':
            h=self.handles[a]; h[1]=signed(b)+(0,h[1],len(self.files[h[0]]))[c]; ret=0
        elif name=='ftell': ret=self.handles[a][1]
        elif name=='fclose': self.handles.pop(a)
        elif name=='access': ret=0 if self.text(a) in self.files or self.text(a) in self.tree else -1
        elif name=='rename': self.files[self.text(b)]=self.files.pop(self.text(a))
        elif name=='unlink': self.files.pop(self.text(a),None)
        elif name=='memcmp': ret=0 if bytes(u.mem_read(a,c))==bytes(u.mem_read(b,c)) else 1
        elif name=='strlen': ret=len(self.text(a).encode())
        elif name=='strstr':
            i=self.text(a).encode().find(self.text(b).encode()); ret=a+i if i>=0 else 0
        elif name=='canvas_measure_text': u.reg_write(UC_MIPS_REG_F0,struct.unpack('<I',struct.pack('<f',BOOK_PITCH*c))[0])
        elif name=='mclGetOutputWay': ret=self.way
        elif name=='fork': ret=self.forked
        elif name=='execl':
            sp=u.reg_read(UC_MIPS_REG_SP); e=self.get(sp+16)  # a volume argv, then its terminator
            self.execs.append((self.text(a),self.text(b),self.text(c),self.text(d),e and (self.text(e),self.get(sp+20)))); ret=-1
        elif name=='waitpid': assert a==self.forked and c==1; ret=self.waited
        elif name=='socket': assert (a,b,c)==(1,1,0); ret=7
        elif name=='sendto':
            sp=u.reg_read(UC_MIPS_REG_SP); to=self.get(sp+16)
            self.sent.append((a,bytes(self.u.mem_read(b,c)).decode('latin1'),c,d,self.get(to)&0xffff,self.text(to+2),self.get(sp+20)))
        elif name=='canvas_draw_text':
            sp=u.reg_read(UC_MIPS_REG_SP)
            self.lines.append((''.join(chr(self.get(b+4*j)) for j in range(c)),signed(d),signed(self.get(sp+16)),
                               self.get(self.lcd+O['LCD_TEXT_COLOR']),self.font))
        self.calls.append((name,a,b,c))
        for r in [UC_MIPS_REG_V1,*REGS,UC_MIPS_REG_T8,UC_MIPS_REG_T9]: u.reg_write(r,0xdeadbeef)
        u.reg_write(UC_MIPS_REG_V0,ret&0xffffffff); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))

# Books (patch/books.c): Local Music's last row opens the card's Books folder (case aside): its own
# .txt and .epub files and its subfolders', by path, named without the extension.
import io, zipfile
BOOK_PITCH=9
R='/mnt/mmc/books'
para=lambda i: ' '.join(f'w{i}x{k}' for k in range(30))
txt='﻿'+'\n\n'.join(para(i) for i in range(40))
buf=io.BytesIO()
with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
    z.writestr('mimetype','application/epub+zip')
    z.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
    z.writestr('content.opf','<package><manifest><item id="a" href="a.xhtml"/><item id="b" href="b.xhtml"/></manifest>'
               '<spine><itemref idref="a"/><itemref idref="b"/></spine></package>')
    z.writestr('a.xhtml','<html><head><title>T</title></head><body><h1>Chapter 1</h1><p>Caf&#233; &amp; tea.</p></body></html>')
    z.writestr('b.xhtml','<html><body><p>Chapter 2</p></body></html>')
tree={'/mnt/mmc':[('Music',4),('books',4)],R:[('Zed.txt',8),('notes.md',8),('._Zed.txt',8),('Series',4)],R+'/Series':[('A Tale.EPUB',8)]}
files={R+'/Zed.txt':bytearray(txt.encode()),R+'/Series/A Tale.EPUB':bytearray(buf.getvalue())}
m=BooksMachine(tree,files)
view=m.node('scroll_view','scroll_view_localmusic',[m.node('list_item') for _ in range(11)]); m.top=m.node('window','localmusic_page',[view])
assert m.call(address=HOOKS['localmusic_page_init'][0],args=(m.top,5,0,0),gap=0,count=5_000_000)==0
button=m.nodes[m.nodes[view]['children'][-1]]['children'][0]; icon,label=m.nodes[button]['children']
assert m.nodes[icon]['image']=='local_books' and m.nodes[label]['text']=='Books' and len(m.nodes[view]['children'])==14
f,ctx=m.handler(button,O['EVT_CLICK']); assert m.call(address=f,args=(ctx,m.event,0,0),gap=0,count=5_000_000)==0
page=m.page=m.top; assert m.nodes[page]['name']=='books_page' and m.nodes[page]['style:normal:bg_color']==-0x1000000
books,reader=m.nodes[page]['children']; sheet,info=m.nodes[reader]['children']
assert not m.nodes[reader]['visible'] and labels(books)==['Books','A Tale','Zed']; passed()
# A .txt opens on its first page: up to ten white lines of the default font, the BOM dropped,
# wrapped at spaces within the margins, every line clear of the glass's rounded corners.
rows=m.nodes[m.find('scroll_view',books)]['children']
def open_book(i):
    f,ctx=m.handler(rows[i],O['EVT_CLICK']); m.call(address=f,args=(ctx,m.event,0,0),gap=0); m.advance(0)
def page_lines():
    m.lines=[]; m.call(address=HOOKS['widget_on_paint_border'][0],args=(sheet,m.canvas,0,0),gap=0,clear=False,count=50_000_000)
    return m.lines
open_book(1); assert m.nodes[reader]['visible'] and not m.nodes[books]['visible'] and not m.nodes[info]['visible']
first=page_lines(); words=txt[1:].split()
assert len(first)==10 and all(c==0xffffffff and font==('default',20) for _,_,_,c,font in first)
assert ' '.join(t for t,*_ in first if t).split()==words[:len(' '.join(t for t,*_ in first).split())] and first[0][0].startswith('w0x0 ')
for t,x,y,*_ in first:
    top,bottom=30+y,30+y+26; inset=max(corner_inset(top),corner_inset(bottom))
    assert inset<=x and x+BOOK_PITCH*len(t)<=375-inset and len(t)*BOOK_PITCH<=375-2*x,(t,x,y)
assert [y for _,_,y,*_ in first]==[6+26*i for i in range(10)] and len(first[0][0])*BOOK_PITCH>375-2*24-BOOK_PITCH*6; passed()
# The wheel turns the page (the next starts where this one ended), back again, and stops at the start.
m.top=page; assert m.call()==11; second=page_lines()
seen=' '.join(t for t,*_ in first+second).split(); assert seen==words[:len(seen)] and second[0][0]!=first[0][0]
assert m.call(O['KEY_PREV'])==11 and page_lines()==first and m.call(O['KEY_PREV'])==11 and page_lines()==first; passed()
# Centre shows how far in and the title on a band clear of the glass; Return keeps the page.
m.call(); m.call(); third=page_lines()
assert m.call(O['KEY_CENTER'])==11 and m.nodes[info]['visible'] and re.fullmatch(r'\d%  Zed',m.nodes[info]['text'])
x,y,bw,bh=cf_geometry(m,info); inset=max(corner_inset(30+y),corner_inset(30+y+bh)); assert inset<=x and x+bw<=375-inset
assert m.key()==11 and m.nodes[books]['visible'] and '/mnt/data/ringnav-books' in m.files
assert m.nodes[m.find('scroll_view',books)]['_ringnav_index']==1
open_book(1); assert page_lines()==third; passed()
# An EPUB is made into text on the worker first ("Preparing…"), then read like a .txt: chapters
# on new pages, head dropped, entities decoded.
m.key(); open_book(0); assert m.nodes[info]['visible'] and m.nodes[info]['text']=='Preparing…' and len(m.threads)==1 and not page_lines()
worker,arg=m.threads[0]; assert m.call(address=worker,args=(arg,0,0,0),gap=0,count=50_000_000)==0
m.advance(250); cache=f'/mnt/mmc/.books/{fnv(R+"/Series/A Tale.EPUB"):08x}.txt'
assert bytes(m.files[cache])=='Chapter 1\n\nCafé & tea.\f\nChapter 2'.encode() and not m.nodes[info]['visible']
assert [t for t,*_ in page_lines()]==['Chapter 1','','Café & tea.'] and m.call()==11 and [t for t,*_ in page_lines()]==['Chapter 2']
passed()
# A broken EPUB says so; closing joins nothing and frees the window.
m.key(); m.files[R+'/Series/A Tale.EPUB']=bytearray(b'PK junk'); m.files.pop(cache); open_book(0)
worker,arg=m.threads[-1]; m.call(address=worker,args=(arg,0,0,0),gap=0,count=50_000_000); m.advance(250)
assert m.nodes[info]['visible'] and m.nodes[info]['text']=="Can't open this book" and m.call()==11 and not page_lines()
m.close(); passed()

# Videos (patch/books.c, docs/internals.md#videos): the card's Videos folder (case aside) lists its and
# its subfolders' video files as Books does; a row stops the music and starts /usr/bin/q2video with
# the headphone DAC's PCM and the path as plain argv, never through a shell.
R='/mnt/mmc/VIDEOS'
tree={'/mnt/mmc':[('Music',4),('VIDEOS',4)],R:[('b.MKV',8),('a.mp4',8),('notes.txt',8),('._a.mp4',8),('Trip',4)],R+'/Trip':[('c.avi',8)]}
m=BooksMachine(tree,{})
view=m.node('scroll_view','scroll_view_localmusic',[m.node('list_item') for _ in range(11)]); m.top=m.node('window','localmusic_page',[view])
assert m.call(address=HOOKS['localmusic_page_init'][0],args=(m.top,5,0,0),gap=0,count=5_000_000)==0
button=m.nodes[m.nodes[view]['children'][-1]]['children'][0]; icon,label=m.nodes[button]['children']
assert m.nodes[icon]['image']=='local_videos' and m.nodes[label]['text']=='Videos' and len(m.nodes[view]['children'])==14
f,ctx=m.handler(button,O['EVT_CLICK']); assert m.call(address=f,args=(ctx,m.event,0,0),gap=0,count=5_000_000)==0
page=m.page=m.top; videos,_=m.nodes[page]['children']
assert m.nodes[page]['name']=='books_page' and labels(videos)==['Videos','c','a','b']; passed()
rows=m.nodes[m.find('scroll_view',videos)]['children']
def play(i):
    m.calls=[]; f,ctx=m.handler(rows[i],O['EVT_CLICK']); assert m.call(address=f,args=(ctx,m.event,0,0),gap=0)==0
    return [c[0] for c in m.calls]
m.forked=0; names=play(1)  # the child
assert names.index('player_stop')<names.index('fork') and 'mclSetDacPwr' not in names and ('exit',127) in [c[:2] for c in m.calls]
assert m.execs==[('/usr/bin/q2video','/usr/bin/q2video','plughw:1,0',R+'/a.mp4',0)]; passed()
# A DAC check_dacoff_state powered off is powered on first. Bluetooth (way 1) plays on hciplayer's
# plug:bluealsa with the volume for the helper's soft volume. A USB DAC (2) plays on hciplayer's
# hw:2,0, through plughw: with the volume marked h (the DAC's own control, or a fixed USB volume),
# or plain when the USB volume is variable and the DAC has no control (USB_MIXER -2: hciplayer's soft volume).
m.word(syms['g_dacoff_time'],0xffffffff); assert 'mclSetDacPwr' in play(2) and m.execs[-1][2:5]==('plughw:1,0',R+'/b.MKV',0)
m.word(syms['g_dacoff_time'],5); m.byte(syms['g_volume'],42)
m.way=1; assert 'mclSetDacPwr' not in play(0) and m.execs[-1][2:]==('plug:bluealsa',R+'/Trip/c.avi',('42',0))
m.way=2; m.byte(syms['g_usbvol_mode'],0); m.word(O['USB_MIXER'],0xfffffffe)
assert 'mclSetDacPwr' not in play(0) and m.execs[-1][2:]==('plughw:2,0',R+'/Trip/c.avi',('h42',0))
m.byte(syms['g_usbvol_mode'],1); m.word(O['USB_MIXER'],0); play(0); assert m.execs[-1][2:]==('plughw:2,0',R+'/Trip/c.avi',('h42',0))
m.word(O['USB_MIXER'],0xfffffffe); play(0); assert m.execs[-1][2:]==('plughw:2,0',R+'/Trip/c.avi',('42',0))
passed()
# Playing: no key or touch reaches the UI; a key's release goes to the player's socket as a datagram:
# Play/Pause pauses, the side buttons seek, Return quits.
m.way=0; m.forked=4242; play(1)
inp=HOOKS['window_manager_dispatch_input_event'][0]; m.handlers[inp+12]='stock_input'
def event(kind,key=0):
    m.calls=[]; m.sent=[]
    assert m.call(key,address=inp,args=(m.wm,m.event,0,0),event_type=kind,gap=0,clear=False)==0
    return [c[0] for c in m.calls if c[0]!='time_now_ms']  # the hook times Low power's idle, not input
for key,c in ((O['KEY_PLAY'],'p'),(O['KEY_FWD_BTN'],'f'),(O['KEY_BACK_BTN'],'b'),(O['KEY_RETURN'],'q')):
    assert 'stock_input' not in event(O['EVT_KEY_UP'],key) and m.sent==[(7,c,1,0x40,1,'/tmp/q2video.sock',110)]
assert event(0x110,O['KEY_CENTER'])==[] and event(O['EVT_POINTER_DOWN'])==[] and not m.sent; passed()
# The wheel is the volume, as stock's volume dialog steps it: 1 a tick through device_set_volume, saved as
# PLAYSET VOLUME, up to g_maxvolume (and 100) and down to 0; each tick sends v and the volume, at an end too.
def key(k):
    names=event(O['EVT_KEY_UP'],k); assert 'stock_input' not in names and len(m.sent)==1 and m.sent[0][2]==len(m.sent[0][1])
    vol=[c[1:3] for c in m.calls if c[0]=='device_set_volume']; saved=[(c[1],m.text(c[2]),m.text(c[3])) for c in m.calls if c[0]=='write_int_config']
    assert len(vol)==len(saved)<=1 and all(v==(s[0],1) and s[1:]==('PLAYSET','VOLUME') for v,s in zip(vol,saved))
    return m.sent[0][1],vol and vol[0][0]
m.byte(syms['g_volume'],50); m.byte(syms['g_maxvolume'],52)
assert [key(O['KEY_NEXT']) for _ in range(3)]==[('v\x33',51),('v\x34',52),('v\x34',[])] and m.u.mem_read(syms['g_volume'],1)[0]==52
m.byte(syms['g_maxvolume'],100); m.byte(syms['g_volume'],100); assert key(O['KEY_NEXT'])==('v\x64',[])
m.byte(syms['g_volume'],1); assert [key(O['KEY_PREV']) for _ in range(2)]==[('v\0',0),('v\0',[])]; passed()
# Centre toggles the wheel to seeking (s, then f and b, no volume) and back (v); seeking also ends
# SCRUB_MS after the last tick, on the UI loop's poll.
m.handlers[HOOKS['main_loop_sleep_default'][0]+12]='stock_sleep'
def poll(ms):
    m.now+=ms; m.call(address=HOOKS['main_loop_sleep_default'][0],args=(0x1234,0,0,0),gap=0,clear=False)
m.byte(syms['g_volume'],30)
assert key(O['KEY_CENTER'])==('s',[]) and key(O['KEY_NEXT'])==('f',[]) and key(O['KEY_PREV'])==('b',[])
poll(O['SCRUB_MS']-1); assert key(O['KEY_NEXT'])==('f',[]) and key(O['KEY_FWD_BTN'])==('f',[]) and key(O['KEY_PLAY'])==('p',[])
poll(O['SCRUB_MS']-1); assert key(O['KEY_NEXT'])==('f',[]); poll(O['SCRUB_MS']); assert key(O['KEY_NEXT'])==('v\x1f',31)
assert key(O['KEY_CENTER'])==('s',[]) and key(O['KEY_CENTER'])==('v\x1f',[]) and key(O['KEY_PREV'])==('v\x1e',30); passed()
# Nor does the window manager paint over it; the screen, standby and DAC power-off timeouts are held off.
vt=m.alloc(16); m.word(m.wm+0x94,vt); m.word(vt+0xc,0x400100); m.handlers[0x400100]='wm_vt_paint'
def wm_paint():
    m.calls=[]; m.call(address=WM_PAINT_LEAF[1],args=(m.wm,0,0,0),gap=0,clear=False); return [c[0] for c in m.calls]
assert wm_paint()==[]
sleep_hook=HOOKS['main_loop_sleep_default'][0]; m.handlers[sleep_hook+12]='stock_sleep'
m.calls=[]; m.call(address=sleep_hook,args=(0x1234,0,0,0),gap=0,clear=False)
assert ('reset_poweroptions_timer',1,1,1) in [c[:4] for c in m.calls] and m.get(syms['g_dacoff_time'])==0 and 'widget_invalidate_force' not in [c[0] for c in m.calls]
# Its end: the socket closes, the whole screen repaints once, and the UI has its input and paint again.
m.waited=4242; m.calls=[]; m.call(address=sleep_hook,args=(0x1234,0,0,0),gap=0,clear=False)
assert ('close',7) in [c[:2] for c in m.calls] and ('widget_invalidate_force',m.wm,0) in [c[:3] for c in m.calls]
assert 'stock_input' in event(O['EVT_KEY_UP'],O['KEY_RETURN']) and not m.sent and wm_paint()==['wm_vt_paint']
m.calls=[]; m.call(address=sleep_hook,args=(0x1234,0,0,0),gap=0,clear=False); assert 'waitpid' not in [c[0] for c in m.calls]
m.close(); passed()

# About: FW. Version shows the stock firmware's version again, not the updater tag in demo's
# literal, and a CFW. Version row follows it. Stock's own row builder (0x4bc274) builds Model and FW.
# Version, so the added row is checked against the real stock widgets, geometry and styles.
m=ShuffleMachine(); m.handlers[int(manifest['patch_symbols']['stock_about_trampoline'],16)]='stock_about'
m.mock('strcpy@GLIBC_2.0','widget_set_tr_text')
def tree(w):
    n=m.nodes[w]
    return (n['type'],[m.get(w+O[k]) for k in ('W_X','W_Y','W_W','W_H')],n.get('style'),
            n.get('hscroll_label_set_only_focus'),n.get('hscroll_label_set_ellipses'),[tree(c) for c in n['children']])
view=m.node('scroll_view','scroll_view_about'); m.top=m.node('window','about_page',[view])
for i in range(7):
    row=m.node('list_item'); m.nodes[view]['children'].append(row)
    if i<2: assert m.call(address=0x4bc274,args=(row,i,0,0),gap=0)==0
rows=list(m.nodes[view]['children'])
assert m.call(address=HOOKS['systemset_about_page_init'][0],args=(m.top,5,0,0),gap=0)==0 and m.calls[0][:3]==('stock_about',m.top,5)
kids=m.nodes[view]['children']; assert kids[:2]==rows[:2] and kids[3:]==rows[2:]
fw=m.nodes[m.nodes[rows[1]]['children'][0]]['children'][1]; assert m.nodes[fw]['text']=='V1.32'
item=kids[2]; button=m.nodes[item]['children'][0]; title,value=m.nodes[button]['children']
# Stock's row, title and value widgets alike.
want=tree(m.nodes[rows[1]]['children'][0])
assert m.nodes[item]['style']=='s_listitem_black' and tree(button)==want
assert m.nodes[title]['text']=='CFW. Version' and m.nodes[value]['text']==f"V{VERSION} {'iPod' if variant=='ipod' else 'Stock'}{' dev'*manifest['dev']}"
assert not m.nodes[button].get('handlers') and not m.nodes[button].get('name'); passed()

# Resume: once a second the UI loop polls the playing track; one of RESUME_MIN_S or longer keeps its
# place in a ring written whole to /mnt/data (a .tmp, renamed), every RESUME_SAVE_S of play, after a
# pause and on a track change, and jumps back there when it next starts playing near its beginning.
# A place near either end is forgotten; a short or CUE track, or a paused start, is left alone.
class ResumeMachine(QueueMachine):
    def __init__(self,files=None):
        super().__init__()
        self.files={} if files is None else files; self.open={}; self.play=(0,0); self.seeks=[]; self.opens=self.renames=0
        self.handlers[sleep_hook+12]='stock_sleep'
        for n in ('fopen@GLIBC_2.2','fread@GLIBC_2.0','fwrite@GLIBC_2.0','fclose@GLIBC_2.2','rename@GLIBC_2.0',
                  'access@GLIBC_2.0','time@GLIBC_2.0','player_playtime_and_length','player_seek_time'): self.handlers[syms[n]]='r:'+n
        self.epoch=1700000000
        self.mock('tk_snprintf')
        self.mock('pthread_mutex_lock@GLIBC_2.0','pthread_mutex_unlock@GLIBC_2.0')  # scrobble.c's log lock
    def hook(self,u,address,size,unused):
        name=self.handlers.get(address,'')
        if not name.startswith('r:'): return super().hook(u,address,size,unused)
        name=name[2:].split('@')[0]; a,b,c,d=[u.reg_read(r) for r in REGS]; ret=0; self.calls.append((name,a,b,c))
        if name=='fopen':
            self.opens+=1; path,mode=self.text(a),self.text(b)
            if mode=='rb' and path not in self.files: ret=0
            else: ret=0x2000000+len(self.calls); self.open[ret]=[path,mode,0,b'' if mode=='wb' else self.files.get(path,b'')]
        elif name=='fread':
            f=self.open[d]; data=f[3][f[2]:f[2]+b*c]; f[2]+=len(data); self.u.mem_write(a,data); ret=len(data)//b
        elif name=='fwrite': self.open[d][3]+=bytes(self.u.mem_read(a,b*c)); ret=c
        elif name=='fclose':
            path,mode,_,data=self.open.pop(a)
            if mode in ('wb','ab'): self.files[path]=data
        elif name=='access': ret=0 if self.text(a) in self.files else -1
        elif name=='time': ret=self.epoch
        elif name=='rename': self.renames+=1; self.files[self.text(b)]=self.files.pop(self.text(a))
        elif name=='player_playtime_and_length':
            if self.play[1]: self.word(a,self.play[0]); self.word(b,self.play[1]); ret=1
            else: ret=-1
        elif name=='player_seek_time': self.seeks.append(signed(a)); self.play=(signed(a),self.play[1])
        for r in [UC_MIPS_REG_V1,*REGS,UC_MIPS_REG_T8,UC_MIPS_REG_T9]: u.reg_write(r,0xdeadbeef)
        u.reg_write(UC_MIPS_REG_V0,ret&0xffffffff); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))
    def poll(self,sec,total=3600,pos=None,n=1):  # n polls a second apart, the time standing at sec
        if pos is not None: self.word(O['MCL_POS'],pos)
        for _ in range(n):
            self.play=(sec,total); self.now+=1000
            assert self.call(address=sleep_hook,args=(0x1234,0,0,0),gap=0)==0
            sec=self.play[0]  # where a seek left it
        return self
    def places(self):
        data=self.files.get('/mnt/data/ringnav-resume',b'')
        return [(k,signed(v)) for k,v in struct.iter_unpack('<II',data) if k]
def playing(m,sec,total=3600,pos=None,n=1):  # n polls a second apart while it plays on from sec
    m.poll(sec,total,pos)
    for _ in range(n-1): m.poll(m.play[0]+1,total)
    return m
m=ResumeMachine().poll(0,100,pos=1,n=3)  # a short track: no file read or write
assert not m.opens; m=ResumeMachine()
playing(m,0,pos=0,n=70)
assert len(m.places())==1 and m.places()[0][1]==61 and m.renames==1 and not m.seeks; passed()
m.poll(80,n=3); assert m.places()[0][1]==80 and m.renames==2; passed()  # paused: saved once it stands still
m.poll(80,n=5); assert m.renames==2  # still paused: nothing more to write
playing(m,81,n=5); m.poll(0,pos=1,n=3)  # another track: A's last second is kept
assert m.places()[0][1]==85 and not m.seeks; passed()
m.poll(0,pos=0,n=3); assert not m.seeks  # back to A, paused at its start: waits for Play
playing(m,1,n=3); assert m.seeks==[85] and m.places()[0][1]==85; passed()
m=ResumeMachine(m.files); m.poll(0,pos=0,n=2); playing(m,2,n=1); assert m.seeks==[85]; passed()  # after a reboot
m=ResumeMachine(m.files); m.poll(0,pos=0,n=2); playing(m,O['RESUME_START_S'],n=1); assert not m.seeks; passed()  # already under way
m.poll(3590,n=3); m.poll(0,pos=1,n=2); assert not m.places(); passed()  # finished: forgotten
m=ResumeMachine(); cue=m.items(m.get(syms['mcl_pdeqplaylist']))[0]; m.word(cue+O['REC_CUE_START'],1200)
playing(m,0,pos=0,n=70); assert not m.places() and not m.renames; passed()
m=ResumeMachine()
for i in range(O['RESUME_SLOTS']+2):  # the last is saved by no later change
    m.word(m.items(m.get(syms['mcl_pdeqplaylist']))[0]+O['REC_PATH'],m.string(f'/p/long{i}')); m.poll(100+i,pos=0,n=3)
assert len(m.places())==O['RESUME_SLOTS'] and m.places()[0][1]==100+O['RESUME_SLOTS'] and m.places()[-1][1]==101; passed()

m=ResumeMachine(); r=m.items(m.get(syms['mcl_pdeqplaylist']))[0]  # Podcasts/Audiobooks: any length, never counted
m.word(r+O['REC_PATH'],m.string('/mnt/mmc/audiobooks/Book/01.mp3')); playing(m,0,200,pos=0,n=110)
assert m.places()==[(fnv('/mnt/mmc/audiobooks/Book/01.mp3'),61)] and '/mnt/data/ringnav-plays' not in m.files
assert '/mnt/mmc/.scrobbler.log' not in m.files; passed()

# Play counts: a track over LISTEN_MIN_S counts once half of it, or LISTEN_MAX_S, has been heard,
# seeks aside; a repeat counts again. Counts are written whole to /mnt/data, most recent first, the
# least recently played of the lowest counts replaced when full.
def counts(m): return [(k,n) for k,n in struct.iter_unpack('<II',m.files.get('/mnt/data/ringnav-plays',b'')) if k]
m=ResumeMachine(); playing(m,0,200,pos=0,n=101); assert not counts(m)
playing(m,100,200,n=50); assert counts(m)==[(fnv('/p/A'),1)]; passed()
m.poll(0,200); playing(m,1,200,n=101); assert counts(m)==[(fnv('/p/A'),2)]; passed()  # repeat-one
playing(m,0,200,pos=1,n=5); m.poll(150,200); playing(m,151,200,n=40); assert len(counts(m))==1; passed()  # seeked past half
playing(m,0,20,pos=2,n=25); assert len(counts(m))==1; passed()  # too short
playing(m,0,3600,pos=1,n=242); assert counts(m)[0]==(fnv('/p/B'),1); passed()  # 4 minutes of a long track
log=m.files['/mnt/mmc/.scrobbler.log'].decode().split('\n')  # the same listens, scrobbled
assert log[:3]==['#AUDIOSCROBBLER/1.1','#TZ/UTC','#CLIENT/Q2 Pod'] and log[-1]=='' and len(log)==7
assert log[3]==f'Artist\tAlbum\tA\t\t200\tL\t{1700000000-100}\t' and log[4]==log[3] and log[5].startswith('Artist\tAlbum\tB\t\t3600\tL\t'); passed()
m=ResumeMachine(); r=m.items(m.get(syms['mcl_pdeqplaylist']))[0]
m.word(r+O['REC_ARTIST'],m.string('')); m.word(r+O['REC_TITLE'],m.string('Tab\tbed')); m.word(r+O['REC_TRACK'],7)
playing(m,0,200,pos=0,n=110); assert counts(m) and '/mnt/mmc/.scrobbler.log' not in m.files  # no artist: counted only
m.word(r+O['REC_ARTIST'],m.string('X')); m.poll(0,200); playing(m,1,200,n=110)  # the tag's title, not the file name
assert m.files['/mnt/mmc/.scrobbler.log'].decode().split('\n')[3].startswith('X\tAlbum\tTab bed\t7\t200\t'); passed()
m.word(r+O['REC_TITLE'],m.string('')); m.word(r+O['REC_NAME'],m.string('Song.flac')); m.word(r+O['REC_PATH'],m.string('/p/Song.flac'))
m.poll(0,200); playing(m,1,200,n=110)  # untitled: the file name without its extension
assert m.files['/mnt/mmc/.scrobbler.log'].decode().split('\n')[4].startswith('X\tAlbum\tSong\t7\t200\t'); passed()
m=ResumeMachine(); m.epoch=86400; playing(m,0,200,pos=0,n=110)  # clock never set: counted only
assert counts(m) and '/mnt/mmc/.scrobbler.log' not in m.files; passed()
m=ResumeMachine(); r=m.items(m.get(syms['mcl_pdeqplaylist']))[0]  # CUE tracks of one image: apart
playing(m,0,200,pos=0,n=110); m.word(r+O['REC_CUE_START'],300); m.poll(0,200); playing(m,1,200,n=110)
assert len(counts(m))==2 and counts(m)[1]==(fnv('/p/A'),1); passed()
m=ResumeMachine(); r=m.items(m.get(syms['mcl_pdeqplaylist']))[0]  # a skipped CUE track's time stays its own
playing(m,0,200,pos=0,n=26); m.word(r+O['REC_CUE_START'],300); playing(m,0,200,n=80); assert not counts(m); passed()
full=[(i+1,1 if i==5 else 2) for i in range(O['PLAYS_SLOTS'])]
m=ResumeMachine({'/mnt/data/ringnav-plays':b''.join(struct.pack('<II',*e) for e in full)})
playing(m,0,200,pos=0,n=110); assert counts(m)==[(fnv('/p/A'),1)]+full[:5]+full[6:]; passed()

# Settings rows (docs/internals.md#charge-limit, #low-power, #album-artists): Power management gains
# Charge limit and Low power, Audio settings gains Artists, after the stock rows in the same widgets
# and styles; Centre or tap toggles and saves each. Artists is stock's own PLAYSET ARTISTTYPE.
def settings_page(hook,view_name,config={},stock_rows=2):
    m=QueueMachine(); m.config.update(config)
    m.handlers[int(manifest['patch_symbols'][f'stock_{hook}_trampoline'],16)]='stock_'+hook
    view=m.node('scroll_view',view_name,[m.entry(0) for _ in range(stock_rows)])
    for e in m.nodes[view]['children']: m.word(e+O['W_PARENT'],view)
    m.top=m.node('window','page',[m.node('list_view','list_view',[view])])
    target={'power':'systemset_powermanager_page_init','audioset':'playset_playset_page_init'}[hook]
    assert m.call(address=HOOKS[target][0],args=(m.top,5,0,0),gap=0)==0 and m.calls[0][:3]==('stock_'+hook,m.top,5)
    rows=m.nodes[view]['children'][stock_rows:]
    buttons=[m.nodes[r]['children'][0] for r in rows]; labels=[m.nodes[b]['children'][1] for b in buttons]
    icons=[m.nodes[m.nodes[b]['children'][0]]['image'] for b in buttons]
    def click(i):
        m.calls=[]; f,ctx=m.handler(buttons[i],O['EVT_CLICK'])
        assert m.call(address=f,args=(ctx,m.event,0,0),gap=0)==0
        return [(c[1],m.text(c[2]),m.text(c[3])) for c in m.calls if c[0]=='write_int_config']
    return m,rows,lambda:[m.nodes[l]['text'] for l in labels],icons,click
m,rows,texts,icons,click=settings_page('power','scroll_view_powermanager')
assert len(rows)==3 and icons==['usb_chargeswitch','system_powermanager','system_keylock'] and all(m.nodes[r]['style']=='s_listitem_black' for r in rows)
assert texts()==['Charge limit: Off','Low power: Off','Wake: Double click']
assert click(0)==[(1,'Q2POD','CHARGELIMIT')] and texts()[0]==f"Charge limit: {O['CHARGE_STOP']}%"
assert click(1)==[(1,'Q2POD','LOWPOWER')] and texts()[1]=='Low power: On'
assert click(0)==[(0,'Q2POD','CHARGELIMIT')] and texts()==['Charge limit: Off','Low power: On','Wake: Double click']
assert click(2)==[(1,'Q2POD','SINGLEWAKE')] and texts()[2]=='Wake: Single click'
assert click(2)==[(0,'Q2POD','SINGLEWAKE')] and texts()[2]=='Wake: Double click'; passed()
m,rows,texts,*_=settings_page('power','scroll_view_powermanager',{'CHARGELIMIT':'1','LOWPOWER':'1'})
assert texts()==[f"Charge limit: {O['CHARGE_STOP']}%",'Low power: On','Wake: Double click']; passed()
m,rows,texts,icons,click=settings_page('power','scroll_view_powermanager',{'SINGLEWAKE':'1'})
assert texts()[2]=='Wake: Single click'
assert click(2)==[(0,'Q2POD','SINGLEWAKE')] and texts()[2]=='Wake: Double click'; passed()
m,rows,texts,icons,click=settings_page('audioset','scroll_view_playset',stock_rows=15)
assert len(rows)==1 and icons==['playset_folderjump'] and texts()==['Artists: Artist']
assert click(0)==[(1,'PLAYSET','ARTISTTYPE')] and m.get(syms['artist_type'])==1 and texts()==['Artists: Album Artist']
assert click(0)==[(0,'PLAYSET','ARTISTTYPE')] and m.get(syms['artist_type'])==0; passed()

# The power poll, on the UI loop. Charge limit: the charger stops (switch_charge_enable(0), the
# BQ25890's /CE) at CHARGE_STOP, again whenever it charges meanwhile, and is handed back at
# CHARGE_RESUME: on, or USB mode's own choice while its page is open. Every CHARGE_POLL_MS.
class PowerMachine(ResumeMachine):
    def __init__(self,config={},files=None):
        super().__init__(files); self.config.update(config); self.writes=[]
        for n in ('fflush@GLIBC_2.0','fsync@GLIBC_2.0','fileno@GLIBC_2.0','unlink@GLIBC_2.0','open@GLIBC_2.0','close@GLIBC_2.0'):
            self.handlers[syms[n]]='p:'+n
    def hook(self,u,address,size,unused):
        name=self.handlers.get(address,''); a=u.reg_read(REGS[0])
        if name.startswith('r:fclose') and self.open.get(a,[0,0])[1]=='w':
            path,_,_,data=self.open.pop(a); self.files[path]=data; self.writes.append((path,data)); self.calls.append(('fclose',a,0,0))
            u.reg_write(UC_MIPS_REG_V0,0); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA)); return
        if name.startswith('r:fopen') and self.text(u.reg_read(REGS[1]))=='w':
            ret=0x2000000+len(self.calls); self.open[ret]=[self.text(a),'w',0,b'']; self.calls.append(('fopen',a,0,0))
            u.reg_write(UC_MIPS_REG_V0,ret); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA)); return
        if not name.startswith('p:'): return super().hook(u,address,size,unused)
        self.calls.append((name[2:].split('@')[0],a,0,0))
        if name.startswith('p:unlink'): self.files.pop(self.text(a),None)
        u.reg_write(UC_MIPS_REG_V0,0); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA))
    def pass_(self,ms=O['CHARGE_POLL_MS']):
        self.now+=ms; self.calls=[]
        assert self.call(address=sleep_hook,args=(0x1234,0,0,0),gap=0)==0
        return [c[1] for c in self.calls if c[0]=='switch_charge_enable'],[c[1] for c in self.calls if c[0]=='sleep_ms']
def level(m,pct,charging=0): m.word(syms['g_power_capacity'],pct); m.word(syms['g_power_chargestate'],charging)
m=PowerMachine({'CHARGELIMIT':'1'})
for pct,charging,want in ((79,2,[]),(80,2,[0]),(79,0,[]),(78,2,[0]),(76,0,[]),(75,0,[1]),(79,2,[])):
    level(m,pct,charging); assert m.pass_()[0]==want,(pct,charging); passed()
level(m,90); assert m.pass_(O['CHARGE_POLL_MS']-1)[0]==[] and m.pass_(1)[0]==[0]; passed()  # polled, not every pass
m.open_pages=['usbmode_page']; m.byte(syms['g_usbdac_chargeflag'],0); level(m,70); assert m.pass_()[0]==[0]; passed()
m=PowerMachine(); level(m,95,2); assert m.pass_()[0]==[]; passed()  # the limit off: stock charges
# Low power: CPU1 offline while the screen is off (the first time bracketed by a synced marker),
# back with the screen; the screen-off pass idles LOW_OFF_SLEEP_MS, and a screen-on pass with no input
# or animation for LOW_IDLE_MS idles LOW_IDLE_SLEEP_MS. Off, no file is touched.
CPU1,MARK='/sys/devices/system/cpu/cpu1/online','/mnt/data/q2pod-cpu1'
m=PowerMachine()
m.byte(syms['g_backlight_status'],0); assert m.pass_(1)[1]==[O['SCREEN_OFF_SLEEP_MS']] and not m.writes and not m.opens; passed()
m=PowerMachine({'LOWPOWER':'1'}); m.byte(syms['g_backlight_status'],0)
assert m.pass_(1)[1]==[O['LOW_OFF_SLEEP_MS']] and m.writes==[(MARK,b''),(CPU1,b'0')] and MARK not in m.files
assert [c[0] for c in m.calls if c[0] in ('fsync','unlink')]==['fsync','unlink','fsync']; passed()  # the marker, then its removal, on flash
m.writes=[]; m.byte(syms['g_backlight_status'],1); m.pass_(1); assert m.writes==[(CPU1,b'1')]; passed()
m.writes=[]; m.byte(syms['g_backlight_status'],0); m.pass_(1); assert m.writes==[(CPU1,b'0')]; passed()  # marked once a boot
m.byte(syms['g_backlight_status'],1); m.animating=1; m.pass_(1)
assert m.pass_(O['LOW_IDLE_MS'])[1]==[]; m.animating=0
assert m.pass_(1)[1]==[O['LOW_IDLE_SLEEP_MS']]; passed()
inp=HOOKS['window_manager_dispatch_input_event'][0]; m.handlers[inp+12]='stock_input'
m.call(address=inp,args=(m.wm,m.event,0,0),gap=0); assert m.pass_(1)[1]==[]; passed()  # input: full speed again
# A refused online is retried once a second, not every pass.
class Refused(PowerMachine):
    def hook(self,u,address,size,unused):
        if self.handlers.get(address,'').startswith('r:fwrite') and self.text(u.reg_read(REGS[0]))=='1':
            self.calls.append(('refused',0,0,0)); u.reg_write(UC_MIPS_REG_V0,0); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA)); return
        return super().hook(u,address,size,unused)
m=Refused({'LOWPOWER':'1'}); m.byte(syms['g_backlight_status'],0); m.pass_(1); m.byte(syms['g_backlight_status'],1)
tries=lambda ms:[c[0] for c in m.pass_(ms) and m.calls if c[0]=='refused']
assert tries(1)==['refused'] and tries(500)==[] and tries(500)==['refused']; passed()
# A marker that cannot be written: no offline, so a stall could not go unnoticed.
class Full(PowerMachine):
    def hook(self,u,address,size,unused):
        if self.handlers.get(address,'').startswith('p:fsync'):
            self.calls.append(('fsync',0,0,0)); u.reg_write(UC_MIPS_REG_V0,0xffffffff); u.reg_write(UC_MIPS_REG_PC,u.reg_read(UC_MIPS_REG_RA)); return
        return super().hook(u,address,size,unused)
m=Full({'LOWPOWER':'1'}); m.byte(syms['g_backlight_status'],0); m.pass_(1); m.pass_(1)
assert not any(p==CPU1 for p,_ in m.writes); passed()
# Now Playing keeps stock's pace: its visualizer and progress run on timers.
m=PowerMachine({'LOWPOWER':'1'}); m.top=m.node('window','playing_page'); m.pass_(1)
assert m.pass_(O['LOW_IDLE_MS'])[1]==[]; passed()
# A boot that stalled offlining CPU1 left the marker: the next one renames it and never tries.
m=PowerMachine({'LOWPOWER':'1'},files={MARK:b''}); m.byte(syms['g_backlight_status'],0); m.pass_(1)
assert not m.writes and '/mnt/data/q2pod-cpu1.bad' in m.files and MARK not in m.files; passed()

print(f'{checks} MIPS execution scenarios passed; toolkit services mocked, stock lock filter executed.')
