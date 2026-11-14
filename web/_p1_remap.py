# -*- coding: utf-8 -*-
"""P1: 暗色 -> 杂志编辑风(暖纸) 全站换肤脚本。只动 CSS 层，不动 DOM/JS。"""
import re, sys

FILES = ['home.html', 'index.html', 'sub.html', 'hd.html', 'one.html', 'pipe.html']

ROOT_STD_OLD = ''':root{
  color-scheme:dark;
  --bg:#131210; --panel:#1b1a18; --line:#2a2926; --line-2:#3a3831;
  --ink:#eceae4; --muted:#a8a49b; --dim:#7e7a71;
  --accent:#6b9aff; --accent-2:#242a3e; --ok:#5fd39a; --warn:#e5b05e; --bad:#f07d6e;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}'''
ROOT_STD_NEW = ''':root{
  color-scheme:light;
  --bg:#F5F2EB; --panel:#EDE9DE; --line:#DDD8CB; --line-2:#AAA7A1;
  --ink:#1A1712; --muted:#5D594F; --dim:#94928B;
  --accent:#C13B1B; --accent-2:#EFE0D8; --ok:#3E7A4E; --warn:#A8701D; --bad:#9E2B1B;
  --plate:#14120E; --plate-ink:#E9E4D8; --plate-dim:#9B968A; --plate-line:#35322A;
  --display:Georgia,"Palatino Linotype","Noto Serif SC","SimSun",serif;
  --mono:Consolas,"Cascadia Mono",ui-monospace,"SF Mono",monospace;
}'''
ROOT_IDX_OLD = ''':root{
    color-scheme:dark;
    --bg:#131210; --panel:#1b1a18; --line:#2a2926; --line-2:#3a3831;
    --ink:#eceae4; --muted:#a8a49b; --faint:#7e7a71;
    --accent:#6b9aff; --accent-soft:#242a3e; --accent-line:#3d517e;
    --ok:#5fd39a; --warn:#e5b05e; --err:#f07d6e;
    --mono:ui-monospace,"Cascadia Mono",Consolas,"SF Mono",monospace;
    --sans:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  }'''
ROOT_IDX_NEW = ''':root{
    color-scheme:light;
    --bg:#F5F2EB; --panel:#EDE9DE; --line:#DDD8CB; --line-2:#AAA7A1;
    --ink:#1A1712; --muted:#5D594F; --faint:#94928B;
    --accent:#C13B1B; --accent-soft:#EFE0D8; --accent-line:#CE8465;
    --ok:#3E7A4E; --warn:#A8701D; --err:#9E2B1B;
    --plate:#14120E; --plate-ink:#E9E4D8; --plate-dim:#9B968A; --plate-line:#35322A;
    --display:Georgia,"Palatino Linotype","Noto Serif SC","SimSun",serif;
    --mono:Consolas,"Cascadia Mono",ui-monospace,"SF Mono",monospace;
    --sans:"Microsoft YaHei",system-ui,-apple-system,"Segoe UI",sans-serif;
  }'''

# 预替换：保暗/反转的精确串（先于全局映射执行）
PRE = [
    # toast: 亮色界面上保留墨块 toast + 纸字（编辑风点缀）
    ('background:#26251f;color:#eceae4;border:1px solid #3a3831',
     'background:#1A1712;color:#F5F2EB;border:1px solid #1A1712', 3),
    # 日志控制台: 保暗版
    ('background:#171614;border:1px solid var(--line);border-radius:10px;padding:10px 12px;max-height:180px;overflow:auto;font:11px var(--mono);color:#b3afa6',
     'background:#14120E;border:1px solid #35322A;border-radius:10px;padding:10px 12px;max-height:180px;overflow:auto;font:11px var(--mono);color:#9B968A', 2),
    # 分段控件选中态: 墨底纸字
    ('background:#3a3831;color:#eceae4', 'background:var(--ink);color:#F5F2EB', 2),
    # sub 滑杆手柄: 纸底红边
    ('background:#eceae4;border:2px solid var(--accent)', 'background:#F5F2EB;border:2px solid var(--accent)', 1),
]

# 全局 hex 映射（角色感知后的统一表）
HEX = {
    # chrome 面
    '#131210': '#F5F2EB', '#1b1a18': '#EDE9DE', '#201f1c': '#E4DFD1',
    '#26251f': '#E8E4D8', '#262521': '#E6E1D4', '#2b2926': '#DDD8CB',
    '#2a2926': '#DDD8CB', '#1d1c1a': '#E4DFD1', '#242420': '#E1DCCF',
    '#2c2c26': '#E1DCCF', '#1e1e18': '#E8E4D8',
    # 文字
    '#eceae4': '#1A1712', '#a8a49b': '#5D594F', '#7e7a71': '#94928B',
    '#b3afa6': '#8A867C', '#cfcdc4': '#C9C6BF', '#d8d6cc': '#C9C6BF',
    '#e8e6de': '#C9C6BF', '#ece9e0': '#C9C6BF',
    '#4a4740': '#B0ACA1', '#57544d': '#A5A196', '#5a5a4e': '#A3A397',
    '#3a3a32': '#B8B4A8', '#8d8b82': '#7E7A70', '#87857c': '#7E7A70',
    '#3c3a35': '#B5B1A6',
    # 蓝系 -> 朱砂
    '#6b9aff': '#C13B1B', '#9db9ff': '#C13B1B', '#8ab4ff': '#A93415',
    '#b9d2ff': '#C13B1B', '#5f83d6': '#CE8465', '#5f7fbd': '#CE8465',
    '#242a3e': '#EFE0D8', '#3d517e': '#CE8465', '#1e2a4a': '#E8D4C9',
    '#232b3d': '#EFE0D8', '#20293a': '#EFE0D8', '#24304a': '#E8D4C9',
    '#2a3757': '#E8D4C9', '#2c3a5c': '#E8D4C9',
    # 语义
    '#5fd39a': '#3E7A4E', '#1c2b22': '#DFE4D7', '#2e5c42': '#7FA98B',
    '#cfe6d9': '#DFE4D7', '#3f7a58': '#3E7A4E',
    '#e5b05e': '#A8701D', '#33290f': '#EBE2D2', '#6e571f': '#B08A45',
    '#f07d6e': '#9E2B1B', '#362019': '#F1DFD8', '#6e352c': '#B4573F',
    '#f0938a': '#9E2B1B',
    # 反色按钮文字
    '#171614': '#F5F2EB',
}
# 注意: #171614 只允许出现在 color: 位 —— 用带前缀的替换; .log 的 background 已被预替换处理

RGBA = {
    'rgba(107,154,255,.30)': 'rgba(193,59,27,.30)',
    'rgba(107,154,255,.13)': 'rgba(193,59,27,.13)',
    'rgba(107,154,255,.10)': 'rgba(193,59,27,.10)',
    'rgba(107,154,255,.20)': 'rgba(193,59,27,.20)',
    'rgba(107,154,255,.14)': 'rgba(193,59,27,.14)',
    'rgba(107,154,255,.16)': 'rgba(193,59,27,.16)',
    'rgba(78,196,140,.18)': 'rgba(62,122,78,.16)',
    'rgba(46,150,96,.95)': 'rgba(62,122,78,.95)',
}

EDITORIAL_LAYER = '''
/* ---- editorial P1 layer ---- */
h1,h2,h3{font-family:var(--display);letter-spacing:.01em}
::selection{background:#C13B1B;color:#F5F2EB}
'''

for f in FILES:
    s = open(f, encoding='utf-8').read()
    orig = s

    # 1) :root 整块替换
    if f == 'index.html':
        assert s.count(ROOT_IDX_OLD) == 1, f + ': index :root 不匹配'
        s = s.replace(ROOT_IDX_OLD, ROOT_IDX_NEW)
    else:
        n = s.count(ROOT_STD_OLD)
        assert n == 1, f + ': std :root 不匹配 ({n})'.format(n=n)
        s = s.replace(ROOT_STD_OLD, ROOT_STD_NEW)

    # 2) 精确预替换
    for old, new, expect in PRE:
        got = s.count(old)
        # expect 是全站总命中数, 单文件可为 0
        s = s.replace(old, new)
        PRE_local = got

    # 3) color:#171614 仅 color 位
    s = s.replace('color:#171614', 'color:#F5F2EB')

    # 4) 全局 hex（大小写不敏感逐个处理）
    total = 0
    for old, new in HEX.items():
        if old == '#171614':
            continue
        pat = re.compile(re.escape(old), re.IGNORECASE)
        s, k = pat.subn(new, s)
        total += k

    # 5) rgba
    for old, new in RGBA.items():
        s = s.replace(old, new)

    # 6) color-scheme 残留
    s = s.replace('color-scheme:dark', 'color-scheme:light')
    s = s.replace('content="dark"', 'content="light"')

    # 7) 编辑风层注入（只注入一次）
    if 'editorial P1 layer' not in s:
        i = s.rfind('</style>')
        assert i > 0, f + ': 无 </style>'
        s = s[:i] + EDITORIAL_LAYER + s[i:]

    open(f, 'w', encoding='utf-8', newline='').write(s)
    print('{0}: hex 替换 {1} 处, 长度 {2}->{3}'.format(f, total, len(orig), len(s)))

print('P1 remap 完成')
