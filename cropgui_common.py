#    a part of cropgui, a graphical front-end for lossless jpeg cropping
#    Copyright (C) 2009 Jeff Epler <jepler@unpythonic.net>
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program; if not, write to the Free Software
#    Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
from collections import namedtuple

from PIL import Image
from PIL import ImageFilter
from PIL import ImageDraw
import platform
import subprocess
import threading
import queue
import os
import math

def getoutput(c):
    p = subprocess.Popen(c, shell=isinstance(c, str), stdout=subprocess.PIPE)
    stdout, stderr = p.communicate()
    return stdout


def _(s): return s  # TODO: i18n

(
    DRAG_NONE,
    DRAG_TL, DRAG_T, DRAG_TR,
    DRAG_L,  DRAG_C, DRAG_R,
    DRAG_BL, DRAG_B, DRAG_BR
) = list(range(10))


POPULAR_FORMATS = ((1, 1), (4, 3), (3, 2), (16, 9))

def closest_ratio(ratio, formats=POPULAR_FORMATS):
    n, d = min(formats, key=lambda f: abs(f[0]/f[1]-ratio))	# find the format closest to the ratio
    s = ratio*d-n
    return f'{n}{s:+.2f}', d

def describe_ratio(a, b):
    if a == 0 or b == 0: return "degenerate"
    if a > b:
        ratio = a*1./b
        f,d = closest_ratio(ratio)
        return f'{ratio:.2f}:1 | {f}:{d}'
    else:
        ratio = b*1./a
        f,d = closest_ratio(ratio)
        return f'1:{ratio:.2f} | {d}:{f}'

def clamp(value, low, high):
    if value < low: return low
    if high < value: return high
    return value

def nextPowerOf2(n):
    count = 0;
    ceiln = math.ceil(n)
    # First ceiln in the below condition is for the
    # case where ceiln is 0
    if (ceiln and not(ceiln & (ceiln - 1))):
        return ceiln

    while (ceiln != 0):
        ceiln >>= 1
        count += 1

    return 1 << count;


def get_cropspec(image, corners, rotation):
    t, l, r, b = corners
    w = r - l
    h = b - t
    # Technically these parameters should straightforwardly produce perfect
    # crops, but jpegtran is broken here in two regards: (1) it doesn't
    # recognise perfect rotated crops as such, so the '-perfect' switch
    # erroneously rejects correctly ICMU-aligned rotate-and-crop commands, and
    # (2) it mistakenly rounds out crops on rotated/flipped images so that even
    # the bottom right corner is ICMU-aligned.  Sigh.  Adding the 'f' suffixes
    # to the dimensions here at least solves (2), and seems to produce the same
    # results as you get by manually constructing a pipeline of '-perfect'
    # command lines.
    if image.format == "JPEG" or image.format == "MPO":
        return "%dfx%df+%d+%d" % (w, h, l, t)
    else:
        return "%dx%d+%d+%d" % (w, h, l, t)


def ncpus():
    if os.path.exists("/proc/cpuinfo"):
        return open("/proc/cpuinfo").read().count("bogomips") or 1
    return 1
ncpus = ncpus()


CropRequest = namedtuple(
    "CropRequest",
    ["image", "image_name", "corners", "rotation", "target"]
)


class CropTask(object):
    def __init__(self, log):
        self.log = log
        self.tasks = queue.Queue()
        self.threads = set(self.create_task() for i in range(ncpus))
        for t in self.threads: t.start()

    def done(self):
        for t in self.threads:
            self.tasks.put(None)
        for t in self.threads:
            t.join()

    def create_task(self):
        return threading.Thread(target=self.runner)

    def count(self):
        return len(self.tasks) + len(self.threads)

    def add(self, task):
        self.tasks.put(task)

    def runner(self):
        while 1:
            task = self.tasks.get()
            if task is None:
                break
            image = task.image
            image_name = task.image_name
            rotation_int = task.rotation
            target = task.target
            shortname = os.path.basename(target)
            self.log.progress(_("Cropping to %s") % shortname)

            t, l, r, b = task.corners
            cropspec = get_cropspec(image, task.corners, rotation_int)

            if rotation_int == 3:
                rotation = "180"
            elif rotation_int == 6:
                rotation = "90"
            elif rotation_int == 8:
                rotation = "270"
            else:
                rotation = "none"

            # Copy file if no cropping or rotation.
            if (r + b - l - t) == (image.width + image.height) and rotation == "none":
                command = ["nice", "cp", image_name, target]
                if platform.system() == 'Windows': command = ["copy", image_name, target]
            # JPEG crop uses jpegtran
            elif image.format == "JPEG" or image.format == "MPO":
                command = ["nice", "jpegtran"]
                if platform.system() == 'Windows': command = ["jpegtran"]
                if rotation != "none":
                    command += ["-rotate", rotation]
                command += [
                    "-copy", "all",
                    "-crop", cropspec,
                    "-outfile", target,
                    image_name,
                ]
            # All other images use ImageMagick convert.
            else:
                command = ["nice", "convert"]
                if platform.system() == 'Windows': command = ["magick"]
                if rotation != "none":
                    command += ["-rotate", rotation]
                command += [image_name, "-crop", cropspec, '+repage', target]

            print(" ".join(command))
            subprocess.call(command)
            subprocess.call(["exiftool", "-overwrite_original", "-Orientation=1", "-n", target])
            self.log.log(_("Cropped to %s") % shortname)

class DragManagerBase(object):
    def __init__(self):
        self.render_flag = 0
        self.show_handles = True
        self.state = DRAG_NONE
        self.round_x = None
        self.round_y = None
        self.image = None
        self.w = 0
        self.h = 0
        self.left = self.right = self.top = self.bottom = 0
        self.prev_w = self.prev_h = 0
        self.prev_top = self.prev_bottom = 0
        self.prev_left = self=prev_right = 0

    def save_prev_crop(self):
        self.prev_w = self.w
        self.prev_h = self.h
        self.prev_top = self.top
        self.prev_bottom = self.bottom
        self.prev_left = self.left
        self.prev_right = self.right

    def set_image(self, image):
        if image is None:
            # XXX Why are left,right,bottom deleted?
            if hasattr(self, 'left'): del self.left
            if hasattr(self, 'right'): del self.right
            if hasattr(self, 'bottom'): del self.bottom
            if hasattr(self, 'blurred'): del self.blurred
            if hasattr(self, 'xor'): del self.xor
            self._image = None
        else:
            self._orig_image = image.copy()
            self._rotation = 1
            self.image_or_rotation_changed()

    def apply_rotation(self, image):
        if self.rotation == 1: return image.copy()
        if self.rotation == 3: return image.transpose(Image.ROTATE_180)
        if self.rotation == 6: return image.transpose(Image.ROTATE_270)
        if self.rotation == 8: return image.transpose(Image.ROTATE_90)

    def image_or_rotation_changed(self):
        self._image = image = self.apply_rotation(self._orig_image)
        # If new image size matches previous image size then preserve previous crop rect
        if self.w == 0 or self.w != self.prev_w or self.h != self.prev_h:
            self.top, self.bottom = self.fix(0, self.h, self.h, self.round_y, self.rotation in (3, 8))
            self.left, self.right = self.fix(0, self.w, self.w, self.round_x, self.rotation in (3, 6))
        else:
            self.top, self.bottom = self.prev_top, self.prev_bottom
            self.left, self.right = self.prev_left, self.prev_right
        blurred = image.copy()
        mult = len(self.image.mode) # replicate filter for L, RGB, RGBA
        self.blurred = image.copy().filter(
            ImageFilter.SMOOTH_MORE).point([x//2 for x in range(256)] * mult)
        self.xor = image.copy().point([x ^ 128 for x in range(256)] * mult)
        self.image_set()
        self.render()

    def fix(self, a, b, lim, r, reverse):
        """
        a, b: interval to fix
        lim: upper bound
        r: rounding size
        reverse: True to treat the upper bound as the origin
        """
        a, b = sorted((int(a), int(b)))
        if reverse:
            a = lim - ((((lim - a) + r - 1) // r) * r)
            # Rotation of non-ICU-aligned images can push bounds outside
            # the visible area, and while it might be nice to be able to
            # expose this, I don't know how to; so, crop inwards
            # instead.
            if a < 0:
                a += r
        else:
            a = (a // r) * r
        return a, b

    def get_corners(self):
        return self.top, self.left, self.right, self.bottom

    def get_screencorners(self):
        t, l, r, b = self.get_corners()
        return(int(t/int(self.scale)), int(l/int(self.scale)),
               int(r/int(self.scale)), int(b/int(self.scale)))

    def describe_ratio(self):
        w = self.right - self.left
        h = self.bottom - self.top
        return describe_ratio(w, h)

    def set_stdsize(self, x, y):
        # if frame doesn't fit in image, scale, preserving aspect ratio
        if (x > self.w):
            y = y * self.w / x
            x = self.w
        if (y > self.h):
            x = x * self.h / y
            y = self.h

        # calculate new crop area, preserving center
        left = (self.left + self.right - x) / 2
        right = left + x
        top = (self.top + self.bottom - y) / 2
        bottom = top + y

        # move crop area into the image, if necessary
        if (left < 0):
            left = 0
            right = x
        if (right > self.w):
            right = self.w
            left = right - x
        if (top < 0):
            top = 0
            bottom = y
        if (bottom > self.h):
            bottom = self.h
            top = bottom - y

        self.set_crop (top, left, right, bottom)

    def set_crop(self, top, left, right, bottom):
        self.top, self.bottom = self.fix(top, bottom, self.h, self.round_y, self.rotation in (3, 8))
        self.left, self.right = self.fix(left, right, self.w, self.round_x, self.rotation in (3, 6))
        self.render()

    def get_image(self):
        return self._image
    image = property(get_image, set_image, None,
                "change the target of this DragManager")

    def rendered(self):
        if self.image is None: return None

        t, l, r, b = self.get_screencorners()

        assert isinstance(t, int), t
        assert isinstance(l, int), l
        assert isinstance(r, int), r
        assert isinstance(b, int), b

        mask = Image.new('1', self.image.size, 0)
        mask.paste(1, (l, t, r, b))
        image = Image.composite(self.image, self.blurred, mask)

        if self.show_handles:
            dx = (r - l) / 3
            dy = (b - t) / 3

            mask = Image.new('1', self.image.size, 1)
            draw = ImageDraw.Draw(mask)

            draw.line([l, t, r, t], fill=0)
            draw.line([l, b, r, b], fill=0)
            draw.line([l, t, l, b], fill=0)
            draw.line([r, t, r, b], fill=0)
            
            draw.line([l, t+dy, r, t+dy], fill=0)
            draw.line([l, b-dy, r, b-dy], fill=0)
            draw.line([l+dx, t, l+dx, b], fill=0)
            draw.line([r-dx, t, r-dx, b], fill=0)

            image = Image.composite(image, self.xor, mask)
        return image

    def classify(self, x, y):
        t, l, r, b = self.get_screencorners()
        dx = (r - l) / 4
        dy = (b - t) / 4

        if x < l: return DRAG_NONE
        if x > r: return DRAG_NONE
        if y < t: return DRAG_NONE
        if y > b: return DRAG_NONE

        if x < l+dx:
            if y < t+dy: return DRAG_TL
            if y < b-dy: return DRAG_L
            return DRAG_BL
        if x < r-dx:
            if y < t+dy: return DRAG_T
            if y < b-dy: return DRAG_C
            return DRAG_B
        else:
            if y < t+dy: return DRAG_TR
            if y < b-dy: return DRAG_R
            return DRAG_BR

    cursor_map = {
        DRAG_TL: 'top_left_corner',
        DRAG_L: 'left_side',
        DRAG_BL: 'bottom_left_corner',
        DRAG_TR: 'top_right_corner',
        DRAG_R: 'right_side',
        DRAG_BR: 'bottom_right_corner',
        DRAG_T: 'top_side',
        DRAG_B: 'bottom_side',
        DRAG_C: 'fleur'}

    def drag_start(self, x, y, fixed=False):
        self.x0 = x
        self.y0 = y
        self.t0 = self.top
        self.l0 = self.left
        self.r0 = self.right
        self.b0 = self.bottom
        self.state = self.classify(x, y)
        if self.state in (DRAG_TL, DRAG_TR, DRAG_BL, DRAG_BR):
            self.fixed_ratio = fixed
        else:
            # can't drag an edge and preserve ratio (what does that mean?)
            # dragging center always preserves ratio
            self.fixed_ratio = False

    def drag_continue(self, x, y):
        dx = (x - self.x0) * int(self.scale)
        dy = (y - self.y0) * int(self.scale)
        if self.fixed_ratio:
            ratio = (self.r0-self.l0) * 1. / (self.b0 - self.t0)
            if self.state in (DRAG_TR, DRAG_BL): ratio = -ratio
            if abs(dx/ratio) > abs(dy):
                dy = int(round(dx / ratio))
            else:
                dx = int(round(dy * ratio))
        new_top, new_left, new_right, new_bottom = self.get_corners()
        if self.state == DRAG_C:
            # A center drag bumps into the edges
            if dx > 0:
                dx = min(dx, self.w - self.r0)
            else:
                dx = max(dx, -self.l0)
            if dy > 0:
                dy = min(dy, self.h - self.b0)
            else:
                dy = max(dy, -self.t0)
        if self.state in (DRAG_TL, DRAG_T, DRAG_TR, DRAG_C):
            new_top = self.t0 + dy
        if self.state in (DRAG_TL, DRAG_L, DRAG_BL, DRAG_C):
            new_left = self.l0 + dx
        if self.state in (DRAG_TR, DRAG_R, DRAG_BR, DRAG_C):
            new_right = self.r0 + dx
        if self.state in (DRAG_BL, DRAG_B, DRAG_BR, DRAG_C):
            new_bottom = self.b0 + dy
        # Keep every type of drag within the image bounds
        if new_top < 0:
            new_top = 0
        if new_left < 0:
            new_left = 0
        if new_right >= self.w:
            new_right = self.w-1
        if new_bottom >= self.h:
            new_bottom = self.h-1
        # A drag never moves left past right and so on
        if self.state != DRAG_C:
            new_top = min(self.bottom-1, new_top)
            new_left = min(self.right-1, new_left)
            new_right = max(self.left+1, new_right)
            new_bottom = max(self.top+1, new_bottom)

        self.set_crop(new_top, new_left, new_right, new_bottom)

    def drag_end(self, x, y):
        self.set_crop(self.top, self.left, self.right, self.bottom)
        self.state = DRAG_NONE

    def _flip_dimensions(self):
        self.w, self.h = self.h, self.w
        self.round_x, self.round_y = self.round_y, self.round_x

    def rotate_ccw(self):
        self._flip_dimensions()
        r = self.rotation
        if   r == 1: r = 8
        elif r == 8: r = 3
        elif r == 3: r = 6
        elif r == 6: r = 1
        self.rotation = r

    def rotate_cw(self):
        self._flip_dimensions()
        r = self.rotation
        if   r == 1: r = 6
        elif r == 6: r = 3
        elif r == 3: r = 8
        elif r == 8: r = 1
        self.rotation = r

    inverse = {1: 1, 3:3, 6:8, 8:6}

    def set_rotation(self, rotation):
        if rotation not in (1, 3, 6, 8):
            raise ValueError('Unsupported rotation %r' % rotation)

        print("rotation", self.rotation, "->", rotation)
        self._rotation = rotation
        self.image_or_rotation_changed()

    def get_rotation(self):
        return self._rotation
    rotation = property(get_rotation, set_rotation, None,
            'Set image rotation')

def image_rotation(i):
    if not hasattr(i, '_getexif'):
        print("no getexif?", type(i), getattr(i, '_getexif', None))
        return 1
    exif = i._getexif()
    if not isinstance(exif, dict):
        print("not dict?", repr(exif))
        return 1
    result = exif.get(0x112, None)
    print("image_rotation", result)
    return result or 1


def image_round(i):
    """Return (horizontal block size, vertical block size)"""
    if i.format == "JPEG" or i.format == "MPO":
        x = max(xsamp for _id, xsamp, ysamp, _qtable in i.layer)
        y = max(ysamp for _id, xsamp, ysamp, _qtable in i.layer)
        return x * 8, y * 8
    else:
        return 1, 1


_desktop_name = None
def desktop_name():
    global _desktop_name
    if not _desktop_name:
        _desktop_name = getoutput("""
            test -f ${XDG_CONFIG_HOME:-~/.config}/user-dirs.dirs && . ${XDG_CONFIG_HOME:-~/.config}/user-dirs.dirs
            echo -n ${XDG_DESKTOP_DIR:-$HOME/Desktop}""")
        if (not os.path.isdir(_desktop_name)
                or not os.access(_desktop_name, os.W_OK)):
            _desktop_name = os.expanduser("~")
    return _desktop_name

def output_name(image_name):
    dirname = os.path.dirname(image_name)
    basename = os.path.basename(image_name)
    if not os.access(dirname, os.W_OK):
        image_name = os.path.join(desktop_name(), basename)
    base, ext = os.path.splitext(image_name)
    target = base + "-crop.jpg"
    return target
