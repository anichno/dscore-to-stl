#!/usr/bin/env python3
"""Convert Dentsply Sirona 'triangulation-proto' blobs to binary STL.

The blob is a length-delimited protobuf message:
  field 1 (wire type 2): packed float32, interleaved xyz -> vertices
  field 2 (wire type 2): packed uint32 (varint-encoded) -> triangle indices

Usage:
  convert_triangulation.py BLOB [BLOB...]      # convert raw triangulation blobs
  convert_triangulation.py capture.har          # find triangulationUrl entries
                                                # in a HAR capture, download them,
                                                # then convert each to STL
"""
import json
import re
import struct
import sys
import urllib.request


def read_varint(buf, off):
    v = 0
    n = 0
    while True:
        b = buf[off]
        off += 1
        n += 1
        v |= (b & 0x7F) << (7 * (n - 1))
        if not (b & 0x80):
            break
    return v, off


def parse(data):
    off = 0
    fields = {}
    while off < len(data):
        tag, off = read_varint(data, off)
        field = tag >> 3
        wt = tag & 7
        if wt == 0:
            val, off = read_varint(data, off)
            fields.setdefault(field, []).append(("v", val))
        elif wt == 2:
            ln, off = read_varint(data, off)
            fields.setdefault(field, []).append(("b", data[off:off + ln]))
            off += ln
        elif wt == 1:
            fields.setdefault(field, []).append(("d", data[off:off + 8]))
            off += 8
        elif wt == 5:
            fields.setdefault(field, []).append(("f", data[off:off + 4]))
            off += 4
        else:
            raise ValueError(f"unexpected wire type {wt} at offset {off}")
    return fields


def to_stl(data):
    fields = parse(data)
    raw = fields[1][0][1]
    verts = struct.unpack("<%df" % (len(raw) // 4), raw)
    idx_raw = fields[2][0][1]
    idx = []
    off = 0
    while off < len(idx_raw):
        v, off = read_varint(idx_raw, off)
        idx.append(v)
    if len(idx) % 3 != 0:
        raise ValueError(f"triangle index count not multiple of 3: {len(idx)}")

    nverts = len(verts) // 3
    ntris = len(idx) // 3
    out = bytearray()
    out += b"DentsplySirona triangulation -> STL".ljust(80, b" ")
    out += struct.pack("<I", ntris)
    for i in range(0, len(idx), 3):
        a, b, c = idx[i], idx[i + 1], idx[i + 2]
        p = verts[a * 3:a * 3 + 3]
        q = verts[b * 3:b * 3 + 3]
        r = verts[c * 3:c * 3 + 3]
        u = (q[0] - p[0], q[1] - p[1], q[2] - p[2])
        v = (r[0] - p[0], r[1] - p[1], r[2] - p[2])
        n = (u[1] * v[2] - u[2] * v[1],
             u[2] * v[0] - u[0] * v[2],
             u[0] * v[1] - u[1] * v[0])
        ln = (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) ** 0.5
        if ln:
            n = (n[0] / ln, n[1] / ln, n[2] / ln)
        out += struct.pack("<3f", *n)
        out += struct.pack("<9f", *p, *q, *r)
        out += struct.pack("<H", 0)
    return out, nverts, ntris


def unescape_json(s):
    """Turn \\uXXXX escapes in a raw JSON string back into characters."""
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)


def extract_triangulation_urls(text):
    """Return triangulationUrl values found anywhere in a response body."""
    if not text:
        return []
    urls = []

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "triangulationUrl" and isinstance(v, str):
                    urls.append(v)
                else:
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    try:
        walk(json.loads(text))
    except Exception:
        pass

    if not urls:
        for m in re.finditer(r'"triangulationUrl"\s*:\s*"([^"]+)"', text):
            urls.append(unescape_json(m.group(1)))

    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def blob_name_from_url(url):
    """Derive the storage blob name (last path segment) from a signed URL."""
    path = url.split("?", 1)[0]
    segs = [s for s in path.split("/") if s]
    return segs[-1] if segs else "triangulation"


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    with open(dest, "wb") as f:
        f.write(data)
    return data


def har_triangulation_urls(fn):
    with open(fn, "rb") as f:
        har = json.load(f)
    urls = []
    for entry in har.get("log", {}).get("entries", []):
        content = entry.get("response", {}).get("content", {})
        text = content.get("text") or ""
        if content.get("encoding") == "base64" and text:
            import base64
            try:
                text = base64.b64decode(text).decode("utf-8", "replace")
            except Exception:
                pass
        urls.extend(extract_triangulation_urls(text))
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def convert_blob(data):
    out, nv, nt = to_stl(data)
    return out, nv, nt


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage:")
        print("  convert_triangulation.py BLOB [BLOB...]")
        print("  convert_triangulation.py capture.har")
        sys.exit(1)

    for fn in sys.argv[1:]:
        if fn.lower().endswith(".har"):
            urls = har_triangulation_urls(fn)
            if not urls:
                print(f"{fn}: no triangulationUrl entries found")
                continue
            print(f"{fn}: found {len(urls)} triangulation URL(s)")
            for url in urls:
                name = blob_name_from_url(url)
                print(f"  downloading {name} ...")
                try:
                    data = download(url, name)
                except Exception as e:
                    print(f"    download failed ({e}); skipping")
                    continue
                out, nv, nt = convert_blob(data)
                out_fn = name + ".stl"
                with open(out_fn, "wb") as f:
                    f.write(out)
                print(f"    {name}: {nv} vertices, {nt} triangles -> {out_fn}")
        else:
            data = open(fn, "rb").read()
            out, nv, nt = convert_blob(data)
            out_fn = fn + ".stl"
            with open(out_fn, "wb") as f:
                f.write(out)
            print(f"{fn}: {nv} vertices, {nt} triangles -> {out_fn}")
