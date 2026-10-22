// The tray icon, drawn at runtime in the mode's colour so no image file ships
// with the app: an anti-aliased ring on transparency, encoded as a PNG.

import { crc32, deflateSync } from 'node:zlib'

/** A ring `size` pixels square in `rgb` (0xRRGGBB) on transparency, as PNG bytes. */
export function ringPng(size: number, rgb: number): Buffer {
  const rows: Buffer[] = []
  const c = (size - 1) / 2
  const radius = size * 0.36
  const width = Math.max(1.2, size * 0.13)
  for (let y = 0; y < size; y += 1) {
    const row = Buffer.alloc(1 + size * 4)
    for (let x = 0; x < size; x += 1) {
      const d = Math.abs(Math.hypot(x - c, y - c) - radius)
      const alpha = Math.max(0, Math.min(1, width / 2 + 0.5 - d))
      row.writeUInt32BE(((rgb << 8) | Math.round(alpha * 255)) >>> 0, 1 + x * 4)
    }
    rows.push(row)
  }
  const chunk = (type: string, data: Buffer): Buffer => {
    const head = Buffer.alloc(8)
    head.writeUInt32BE(data.length, 0)
    head.write(type, 4, 'latin1')
    const crc = Buffer.alloc(4)
    crc.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])) >>> 0, 0)
    return Buffer.concat([head, data, crc])
  }
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(size, 0)
  ihdr.writeUInt32BE(size, 4)
  ihdr.writeUInt8(8, 8) // bit depth
  ihdr.writeUInt8(6, 9) // RGBA
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk('IHDR', ihdr), chunk('IDAT', deflateSync(Buffer.concat(rows))), chunk('IEND', Buffer.alloc(0))])
}
