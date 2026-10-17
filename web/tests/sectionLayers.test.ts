import assert from 'node:assert/strict'
import { test } from 'node:test'
import { correlateBoundaries } from '../src/sectionLayers.ts'

type Strip = [number, number, string][]

test('a clean match joins every boundary, across classes of one group', () => {
  const left: Strip = [[0, 1, 'fill'], [1, 4, 'clay'], [4, 9, 'medium_sand'], [9, 12, 'sandstone']]
  const right: Strip = [[0, 2, 'topsoil'], [2, 5, 'marsh_clay'], [5, 7, 'fine_sand'], [7, 8, 'sand'], [8, 15, 'limestone']]
  assert.deepEqual(correlateBoundaries(left, right, 40), [
    { leftDepth: 1, rightDepth: 2, above: 'made ground', below: 'clay' },
    { leftDepth: 4, rightDepth: 5, above: 'clay', below: 'sand' },
    { leftDepth: 9, rightDepth: 8, above: 'sand', below: 'rock' },
  ])
})

test('a unit missing in one hole leaves the boundaries next to it open', () => {
  const left: Strip = [[0, 3, 'clay'], [3, 5, 'gravel'], [5, 9, 'sand'], [9, 12, 'claystone']]
  const right: Strip = [[0, 4, 'clay'], [4, 8, 'sand'], [8, 12, 'claystone']]
  assert.deepEqual(correlateBoundaries(left, right, 40), [{ leftDepth: 9, rightDepth: 8, above: 'sand', below: 'rock' }])
})

test('units in crossing order are not connected', () => {
  const left: Strip = [[0, 3, 'clay'], [3, 6, 'sand']]
  const right: Strip = [[0, 3, 'sand'], [3, 6, 'clay']]
  assert.deepEqual(correlateBoundaries(left, right, 40), [])
})

test('an ambiguous match draws no line', () => {
  const left: Strip = [[0, 2, 'clay'], [2, 4, 'sand'], [4, 6, 'clay']]
  const right: Strip = [[0, 3, 'clay'], [3, 6, 'gravel']]  // which clay of the left hole? either is as good
  assert.deepEqual(correlateBoundaries(left, right, 40), [])
})

test('holes more than 150 m apart are not correlated', () => {
  const strip: Strip = [[0, 3, 'clay'], [3, 6, 'sand']]
  assert.equal(correlateBoundaries(strip, strip, 40).length, 1)
  assert.deepEqual(correlateBoundaries(strip, strip, 151), [])
})
