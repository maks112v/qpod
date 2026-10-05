"""Tk host window for the MIPS callback emulator in test/patch.py."""
import time
import tkinter as tk

WIDTH = 375
HEIGHT = 320
FOOTER_HEIGHT = 24
SCALE = 2
WHEEL_THRESHOLD = 12


class Emulator:
    def __init__(self, root, Machine, offsets, ipod_hooks, confirm_ms):
        self.root = root
        self.Machine = Machine
        self.O = offsets
        self.keydown = ipod_hooks['on_wm_keydown_before_fun'][0]
        self.confirm_ms = confirm_ms
        self.machine = None
        self.surface = 0
        self.entries = []
        self.labels = []
        self.stack = []
        self.captured = False
        self.scroll = 0
        self.last_frame = time.monotonic()
        self.status = 'Click to capture input'

        root.title('Q2 Pod emulator')
        root.configure(bg='#000000')
        root.resizable(False, False)
        self.canvas = tk.Canvas(root, width=WIDTH * SCALE,
                                height=(HEIGHT + FOOTER_HEIGHT) * SCALE,
                                bg='#000000', highlightthickness=0)
        self.canvas.pack()
        root.bind('<Escape>', self.release_capture)
        root.bind('<MouseWheel>', self.on_scroll)
        root.bind('<Button-1>', self.on_click)
        root.bind('<Return>', lambda _: self.capture())
        root.bind('<KeyPress-Left>', lambda _: self.send_button(self.O['KEY_BACK_BTN']))
        root.bind('<KeyPress-Right>', lambda _: self.send_button(self.O['KEY_FWD_BTN']))
        root.bind('<KeyPress-Up>', lambda _: self.wheel_tick(self.O['KEY_PREV']))
        root.bind('<KeyPress-Down>', lambda _: self.wheel_tick(self.O['KEY_NEXT']))
        root.bind('<KeyPress-BackSpace>', lambda _: self.send_button(self.O['KEY_RETURN']))
        root.bind('<KeyPress-space>', lambda _: self.send_button(self.O['KEY_PLAY']))
        root.protocol('WM_DELETE_WINDOW', root.destroy)
        self.open_page('Home', ['Now Playing', 'Library', 'Coverflow', 'Settings'])
        self.frame()

    def open_page(self, title, labels, remember=True):
        if remember and self.machine:
            self.stack.append((self.title, self.labels))
        self.title = title
        self.labels = labels
        self.machine = self.Machine()
        self.surface, self.entries = self.machine.page_list(
            len(labels), height=HEIGHT - 42, extent=max((HEIGHT - 42), len(labels) * 68),
            name='sysset_page')
        self.machine.word(self.surface + self.O['W_W'], WIDTH)
        for index, entry in enumerate(self.entries):
            self.machine.word(entry + self.O['W_Y'], index * 68)
            self.machine.word(entry + self.O['W_W'], WIDTH)
            self.machine.word(entry + self.O['W_H'], 68)
        self.machine.label(self.entries, labels)
        self.machine.on_click = self.activate
        self.machine.paint(self.surface, gap=0)

    def activate(self, entry, _event):
        index = self.entries.index(entry)
        label = self.labels[index]
        pages = {
            'Library': ['Songs', 'Albums', 'Artists', 'Genres', 'Folders', 'Podcasts',
                        'Audiobooks', 'Photos', 'Books', 'Videos'],
            'Settings': ['Display', 'Playback', 'Bluetooth', 'Wi-Fi', 'System'],
            'Songs': [f'Track {number:02d}' for number in range(1, 41)],
            'Albums': [f'Album {number:02d}' for number in range(1, 25)],
            'Artists': [f'Artist {letter}' for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'],
        }
        if label in pages:
            self.open_page(label, pages[label])
        else:
            self.status = f'Activated {label}'

    def capture(self):
        self.captured = True
        self.canvas.configure(cursor='none')
        self.canvas.focus_set()
        self.status = 'Input captured. Escape releases it.'
        self.draw()

    def release_capture(self, _event=None):
        self.captured = False
        self.canvas.configure(cursor='')
        self.status = 'Input released. Click or Return to capture.'
        self.draw()
        return 'break'

    def sync_clock(self):
        now = time.monotonic()
        elapsed = max(0, round((now - self.last_frame) * 1000))
        if elapsed:
            self.machine.advance(elapsed)
            self.last_frame = now

    def input_pair(self, key):
        self.sync_clock()
        self.machine.call(key, address=self.keydown, event_type=self.O['EVT_KEY_DOWN_BEFORE'],
                          gap=0, debounce=True)
        result = self.machine.call(key, event_type=self.O['EVT_KEY_UP_BEFORE'],
                                   gap=0, debounce=True)
        self.machine.paint(self.surface, gap=0)
        return result

    def wheel_tick(self, key):
        self.input_pair(key)
        self.status = 'Wheel next' if key == self.O['KEY_NEXT'] else 'Wheel previous'
        self.draw()

    def send_button(self, key):
        if not self.captured:
            return
        self.input_pair(key)
        if key == self.O['KEY_CENTER']:
            self.machine.advance(self.confirm_ms, clear=False)
        elif key == self.O['KEY_RETURN'] and self.stack:
            title, labels = self.stack.pop()
            self.open_page(title, labels, remember=False)
        self.status = {
            self.O['KEY_CENTER']: 'Centre', self.O['KEY_BACK_BTN']: 'Previous button',
            self.O['KEY_FWD_BTN']: 'Next button', self.O['KEY_PLAY']: 'Play/Pause',
            self.O['KEY_RETURN']: 'Return',
        }.get(key, f'Key {key}')
        self.draw()

    def on_scroll(self, event):
        if not self.captured:
            return
        self.scroll += event.delta
        while abs(self.scroll) >= WHEEL_THRESHOLD:
            direction = 1 if self.scroll > 0 else -1
            self.scroll -= direction * WHEEL_THRESHOLD
            self.wheel_tick(self.O['KEY_PREV'] if direction > 0 else self.O['KEY_NEXT'])
        return 'break'

    def on_click(self, event):
        if not self.captured:
            self.capture()
            return 'break'
        third = WIDTH * SCALE / 3
        key = (self.O['KEY_BACK_BTN'] if event.x < third else
               self.O['KEY_CENTER'] if event.x < third * 2 else self.O['KEY_FWD_BTN'])
        self.send_button(key)
        return 'break'

    def visible_rows(self):
        offset = self.machine.get(self.surface + self.O['SCROLL_Y'])
        for index, label in enumerate(self.labels):
            y = 42 + index * 68 - offset
            if -68 < y < HEIGHT:
                yield label, y

    def draw(self):
        c = self.canvas
        c.delete('all')
        s = SCALE
        c.create_rectangle(0, 0, WIDTH * s, HEIGHT * s, fill='#000000', outline='')
        c.create_text(16 * s, 20 * s, text=self.title, fill='#ffffff', anchor='w',
                      font=('SF Pro Text', 16 * s, 'bold'))
        c.create_text((WIDTH - 12) * s, 20 * s, text='Q2', fill='#ffffff', anchor='e',
                      font=('SF Pro Text', 11 * s))
        c.create_line(0, 40 * s, WIDTH * s, 40 * s, fill='#262626')
        if self.machine.drawn():
            x, y, width, height = self.machine.sel()
            c.create_rectangle(x * s, (42 + y) * s, (x + width) * s,
                               (42 + y + height) * s, fill='#8b1e2d', outline='')
        for label, y in self.visible_rows():
            c.create_text(18 * s, (y + 34) * s, text=label, fill='#ffffff', anchor='w',
                          font=('SF Pro Text', 17 * s))
        c.create_rectangle(0, HEIGHT * s, WIDTH * s, (HEIGHT + FOOTER_HEIGHT) * s,
                           fill='#000000', outline='')
        c.create_line(0, HEIGHT * s, WIDTH * s, HEIGHT * s, fill='#262626')
        c.create_text(10 * s, (HEIGHT + FOOTER_HEIGHT / 2) * s,
                      text=self.status, fill='#a0a0a0', anchor='w',
                      font=('SF Pro Text', 9 * s))
        c.create_text((WIDTH - 10) * s, (HEIGHT + FOOTER_HEIGHT / 2) * s,
                      text='PREV   CENTRE   NEXT',
                      fill='#ffffff', anchor='e', font=('SF Pro Text', 9 * s))

    def frame(self):
        if self.captured:
            self.sync_clock()
        self.draw()
        self.root.after(16, self.frame)


def smoke_test(Machine, offsets, ipod_hooks):
    machine = Machine()
    surface, entries = machine.page_list(20, height=278, extent=1360, name='sysset_page')
    machine.word(surface + offsets['W_W'], WIDTH)
    for index, entry in enumerate(entries):
        machine.word(entry + offsets['W_Y'], index * 68)
        machine.word(entry + offsets['W_W'], WIDTH)
        machine.word(entry + offsets['W_H'], 68)
    machine.paint(surface, gap=0)
    assert machine.sel() == (0, 0, WIDTH, 68)
    key = offsets['KEY_NEXT']
    def tick(gap):
        machine.advance(gap)
        machine.call(key, address=ipod_hooks['on_wm_keydown_before_fun'][0],
                     event_type=offsets['EVT_KEY_DOWN_BEFORE'], gap=0, debounce=True)
        assert machine.call(key, event_type=offsets['EVT_KEY_UP_BEFORE'],
                            gap=0, debounce=True) == 11
        return machine.selected(surface)
    assert [tick(gap) for gap in (1000, 100, 100)] == [1, 1, 2]
    assert len(entries) == 20
    print('Visual emulator smoke test passed: MIPS input timing, overshoot filtering and paint geometry.')


def run(Machine, offsets, ipod_hooks, confirm_ms, smoke=False):
    if smoke:
        smoke_test(Machine, offsets, ipod_hooks)
        return
    root = tk.Tk()
    Emulator(root, Machine, offsets, ipod_hooks, confirm_ms)
    root.mainloop()
