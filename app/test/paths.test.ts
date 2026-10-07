import assert from 'node:assert/strict'
import { test } from 'node:test'

import { endpointPath, isAbsoluteLocal, jarvisHome, userDataOverride } from '../src/main/paths'

const WIN_HOME = 'C:\\Users\\Rotem'
const LINUX_HOME = '/home/rotem'

test('on Windows the home is .jarvis in the profile unless JARVIS_HOME is a drive path', () => {
  const home = (value?: string) => jarvisHome({ JARVIS_HOME: value }, 'win32', WIN_HOME)
  assert.equal(home(), 'C:\\Users\\Rotem\\.jarvis')
  assert.equal(home('E:\\j'), 'E:\\j')
  assert.equal(home(' E:/j '), 'E:/j')
  assert.equal(home('jarvis'), 'C:\\Users\\Rotem\\.jarvis')
  // Network shares and drive-less roots are never used: Claude Code's file access cannot read them.
  assert.equal(home('\\\\server\\share'), 'C:\\Users\\Rotem\\.jarvis')
  assert.equal(home('\\foo'), 'C:\\Users\\Rotem\\.jarvis')
  assert.equal(home('   '), 'C:\\Users\\Rotem\\.jarvis')
})

test('on Linux and macOS JARVIS_HOME must start with /', () => {
  const home = (value?: string) => jarvisHome({ JARVIS_HOME: value }, 'linux', LINUX_HOME)
  assert.equal(home(), '/home/rotem/.jarvis')
  assert.equal(home('/tmp/j'), '/tmp/j')
  assert.equal(home('tmp/j'), '/home/rotem/.jarvis')
  assert.equal(jarvisHome({}, 'darwin', '/Users/rotem'), '/Users/rotem/.jarvis')
})

test('the address file sits in the home app folder, with the platform separator', () => {
  assert.equal(endpointPath('C:\\Users\\Rotem\\.jarvis', 'win32'), 'C:\\Users\\Rotem\\.jarvis\\app\\endpoint.json')
  assert.equal(endpointPath('E:/j', 'win32'), 'E:\\j\\app\\endpoint.json')
  assert.equal(endpointPath('/home/rotem/.jarvis', 'linux'), '/home/rotem/.jarvis/app/endpoint.json')
})

test('isAbsoluteLocal', () => {
  assert.equal(isAbsoluteLocal('D:\\x', 'win32'), true)
  assert.equal(isAbsoluteLocal('/x', 'win32'), false)
  assert.equal(isAbsoluteLocal('/x', 'linux'), true)
  assert.equal(isAbsoluteLocal('C:\\x', 'linux'), false)
})

test('a test userData folder is taken only when absolute', () => {
  assert.equal(userDataOverride({ JARVIS_USER_DATA: '/tmp/u' }, 'linux'), '/tmp/u')
  assert.equal(userDataOverride({ JARVIS_USER_DATA: 'u' }, 'linux'), undefined)
  assert.equal(userDataOverride({}, 'linux'), undefined)
  assert.equal(userDataOverride({ JARVIS_USER_DATA: 'C:\\t\\u' }, 'win32'), 'C:\\t\\u')
  assert.equal(userDataOverride({ JARVIS_USER_DATA: '\\\\server\\u' }, 'win32'), undefined)
})
