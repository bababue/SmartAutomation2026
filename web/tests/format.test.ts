import assert from 'node:assert/strict'
import { test } from 'node:test'
import { fmtDistance, parseDecimal } from '../src/format.ts'

test('a decimal comma reads like a point', () => {
  assert.equal(parseDecimal('1,5'), 1.5)
  assert.equal(parseDecimal(' 2.25 '), 2.25)
})

test('an empty or unreadable entry is no number, not zero', () => {
  assert.equal(parseDecimal(''), null)
  assert.equal(parseDecimal('  '), null)
  assert.equal(parseDecimal('12 m'), null)
})

test('distances switch to km at 1000 m', () => {
  assert.equal(fmtDistance(0.5), '0.5 m')
  assert.equal(fmtDistance(7.25), '7.3 m')
  assert.equal(fmtDistance(999), '999 m')
  assert.equal(fmtDistance(2345), '2.35 km')
})
