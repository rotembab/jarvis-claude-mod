import assert from 'node:assert/strict'
import { test } from 'node:test'
import { inflateSync } from 'node:zlib'

import { ringPng } from '../src/main/tray-icon'

type Png = { width: number; height: number; colorType: number; pixels: Buffer }

/** Reads back the one-IDAT, filter-0 PNG ringPng writes. */
function decode(png: Buffer): Png {
  assert.deepEqual([...png.subarray(0, 8)], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
  let offset = 8
  const chunks = new Map<string, Buffer>()
  while (offset < png.length) {
    const length = png.readUInt32BE(offset)
    const type = png.toString('latin1', offset + 4, offset + 8)
    chunks.set(type, png.subarray(offset + 8, offset + 8 + length))
    offset += 12 + length
  }
  const ihdr = chunks.get('IHDR')
  const idat = chunks.get('IDAT')
  assert.ok(ihdr && idat && chunks.has('IEND'))
  return { width: ihdr.readUInt32BE(0), height: ihdr.readUInt32BE(4), colorType: ihdr.readUInt8(9), pixels: inflateSync(idat) }
}

/** The RGBA of pixel x, y (each row starts with its filter byte). */
function pixel(png: Png, x: number, y: number): { rgb: string; alpha: number } {
  const at = y * (1 + png.width * 4) + 1 + x * 4
  return { rgb: png.pixels.subarray(at, at + 3).toString('hex'), alpha: png.pixels.readUInt8(at + 3) }
}

test('a 16 px ring is an RGBA PNG', () => {
  const png = decode(ringPng(16, 0x2f9bff))
  assert.equal(png.width, 16)
  assert.equal(png.height, 16)
  assert.equal(png.colorType, 6)
  assert.equal(png.pixels.length, 16 * (1 + 16 * 4))
})

test('a 32 px ring for high-DPI screens', () => {
  const png = decode(ringPng(32, 0x2f9bff))
  assert.equal(png.width, 32)
  assert.equal(png.height, 32)
})

test('the centre is clear and the ring is drawn in the colour', () => {
  const png = decode(ringPng(32, 0x2f9bff))
  assert.equal(pixel(png, 16, 16).alpha, 0)
  // The ring's radius is 0.36 of the size: about 11.5 px from the centre (15.5).
  const onRing = pixel(png, 4, 16)
  assert.ok(onRing.alpha > 0)
  assert.equal(onRing.rgb, '2f9bff')
})
