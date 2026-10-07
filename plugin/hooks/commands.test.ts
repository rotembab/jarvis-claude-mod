import { describe, expect, test } from 'claude-code/testing'
import type { Engine as TestEngine } from 'claude-code/testing'

import { STOP_WAIT_MS } from './helper'

import type { World } from './test-harness'
import {
  completeTurn,
  DATA_DIR,
  jarvis,
  runStep,
  startHelper,
  startSession,
  textChunks,
  VENV_PYTHON,
  WINGET_UV,
  world,
} from './test-harness'

const NVCUDA = 'C:\\Windows\\System32\\nvcuda.dll'
/** A helper's hello capabilities once its config takes plainWake. */
const PLAIN_CAPABLE = ['ptt', 'wake', 'wake.plain']
const UV_LOCK = /[\\/]voice[\\/]uv\.lock$/

/** The user speaks, and the engine starts the turn for their words. */
async function voiceTurn($: TestEngine, w: World, text: string, turnId: string): Promise<void> {
  w.lastHelper().event({ type: 'utterance', id: turnId, text, source: 'ptt', durationMs: 1200, language: 'en' })
  await w.settle()
  await $.classic.UserPromptSubmit({ prompt: text })
  await $.turn.start({ text, turnId })
  await w.settle()
}

/** The helper exits when told to shut down, as the real one does. */
function exitOnShutdown(w: World): void {
  w.respond = command => {
    if (command.name === 'shutdown') w.lastHelper().exit(0)
    return { status: 200, body: { ok: true } }
  }
}

describe('/jarvis', () => {
  test('status: the state, the helper and the help', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    const text = await jarvis($, '')
    expect(text).toContain('JARVIS · ready · hold right ctrl to talk')
    expect(text).toContain('Helper 0.1.0 (pid 4242) · speech model large-v3-turbo on cuda · voice Fish Audio default')
    expect(text).toContain(`Data folder: ${DATA_DIR}`)
    expect(text).toContain('/jarvis stop                     stop speaking and cancel the spoken reply')
    expect(text).toContain('Wake word loading · talking over Jarvis interrupts him · echo cancelling on')
    expect(w.helpers()).toHaveLength(1) // a running helper is left alone
    expect(await jarvis($, 'frobnicate')).toContain('Unknown subcommand "frobnicate"')
  })

  test('stop during a voice reply aborts the turn and stops speech', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    await voiceTurn($, w, 'Summarize the changelog', 'turn-1')
    await runStep($, w, 'turn-1', textChunks('Three changes, sir. The first is a fix. '))

    expect(await jarvis($, 'stop')).toBe('Stopped speaking and cancelled the reply.')
    expect(w.aborts).toEqual(['turn-1'])
    expect(w.named('stop').map(command => command.body)).toEqual([{ reason: 'user' }])

    // The engine then ends the turn as aborted: nothing more is spoken or stopped.
    const speaks = w.named('speak').length
    await completeTurn($, 'turn-1', true)
    await w.settle()
    expect(w.named('speak')).toHaveLength(speaks)
    expect(w.named('speak').some(command => command.body.final === true)).toBe(false)
    expect(w.named('stop')).toHaveLength(1)
  })

  test('stop during a typed turn stops speech but never aborts the turn', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    await $.turn.start({ text: 'run the tests', turnId: 'typed-1' })
    expect(await jarvis($, 'stop')).toBe('Stopped speaking.')
    expect(w.aborts).toEqual([])
    expect(w.named('stop').map(command => command.body)).toEqual([{ reason: 'user' }])
  })

  test('stop says when nothing was playing', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    w.respond = command => ({ status: 200, body: command.name === 'stop' ? { ok: true, stopped: false } : { ok: true } })
    expect(await jarvis($, 'stop')).toBe('Nothing was playing.')
    w.respond = command => ({ status: 200, body: command.name === 'stop' ? { ok: true, stopped: true } : { ok: true } })
    expect(await jarvis($, 'stop')).toBe('Stopped speaking.')
  })

  test('stop with no helper says there is nothing to stop', async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    expect(await jarvis($, 'stop')).toBe('Nothing to stop: the voice helper is not running.')
    expect(w.commands).toHaveLength(0)
  })

  test('talk toggles command-driven listening', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    expect(await jarvis($, 'talk')).toContain('Listening.')
    helper.event({ type: 'state', state: 'listening' })
    await w.settle()
    expect(await jarvis($, 'talk')).toBe('Sent.')
    expect(w.named('listen').map(command => command.body)).toEqual([{ action: 'start' }, { action: 'stop' }])
  })

  test('test speaks a test line and reports the helper\'s refusal', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(await jarvis($, 'test')).toBe('Speaking a test line.')
    w.respond = () => ({ status: 503, body: { ok: false, error: { code: 'fish_key_missing', message: 'No Fish Audio API key is set.' } } })
    expect(await jarvis($, 'test')).toBe('The test line failed: No Fish Audio API key is set.')
    expect(w.named('test_voice')).toHaveLength(2)
  })

  test('talk and test explain a helper that is not running', async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    const why = 'The voice helper is not running (JARVIS · not set up · run /jarvis setup).'
    expect(await jarvis($, 'talk')).toBe(why)
    expect(await jarvis($, 'test')).toBe(why)
  })

  test('devices shows what the helper reports', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    w.respond = command =>
      command.name === 'status'
        ? {
            status: 200,
            body: {
              ok: true,
              state: 'sleeping',
              version: '0.1.0',
              platform: 'windows',
              sttModel: 'large-v3-turbo',
              sttDevice: 'cuda',
              inputDevice: 'Headset Microphone',
              outputDevice: 'Speakers (Realtek)',
              pttKey: 'right ctrl',
              fishKeySet: true,
              wakeWord: 'Hey Jarvis',
              bargeIn: 'speech',
              echoCancel: 'loading',
            },
          }
        : { status: 200, body: { ok: true } }
    const text = await jarvis($, 'devices')
    expect(text).toContain('Wake word: Hey Jarvis · barge-in: speech · echo cancelling: loading')
    expect(text).toContain('Microphone: Headset Microphone')
    expect(text).toContain('Speakers: Speakers (Realtek)')
    expect(text).toContain('Speech model: large-v3-turbo on cuda')
    expect(text).toContain('Voice: Fish Audio default · Fish Audio key set')
    // PowerShell runs a quoted program path only after its call operator.
    expect(text).toContain(`Full report: & "${VENV_PYTHON}" -m jarvis_voice doctor`)
  })

  test('restart stops the helper and starts a new one', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    exitOnShutdown(w)
    expect(await jarvis($, 'restart')).toBe('Restarting the voice helper.')
    await w.settle()
    expect(w.named('shutdown')).toHaveLength(1)
    expect(w.helpers()).toHaveLength(2)
    expect(w.status()).toBe('JARVIS · starting')
  })
})

describe('/jarvis voice', () => {
  test('sets, shows and resets the voice, telling the running helper', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(await jarvis($, 'voice test-voice-0123456789')).toBe('Voice set to test-voice-0123456789.')
    expect(w.store.get('voiceId')).toBe('test-voice-0123456789')
    expect(w.named('config').at(-1)?.body).toEqual({ voiceId: 'test-voice-0123456789' })
    expect(await jarvis($, 'voice')).toContain('Voice: test-voice-0123456789.')

    expect(await jarvis($, 'voice default')).toBe('Voice set to the Fish Audio default voice.')
    expect(w.store.has('voiceId')).toBe(false)
    expect(w.named('config').at(-1)?.body).toEqual({ voiceId: '' })

    expect(await jarvis($, 'voice ../../etc')).toBe('"../../etc" does not look like a Fish Audio model id.')
  })

  test('a saved voice reaches the helper when it starts', async ($, on) => {
    const w = world(on)
    w.store.set('voiceId', 'saved-voice')
    await startHelper($, w)
    expect(w.named('config')[0]?.body).toMatchObject({ pttKey: 'right ctrl', language: 'en', voiceId: 'saved-voice' })
  })

  test('without a running helper the voice is saved for later', async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    expect(await jarvis($, 'voice my-voice')).toBe('Voice set to my-voice; it applies when the helper starts.')
    expect(w.store.get('voiceId')).toBe('my-voice')
    expect(w.commands).toHaveLength(0)
  })
})

describe('/jarvis setup', () => {
  test('without uv it prints the install command and runs nothing', async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    const text = await jarvis($, 'setup')
    await w.settle()
    expect(text).toContain('uv is not installed')
    expect(text).toContain('winget install astral-sh.uv')
    expect(w.checked).toContain('C:\\Users\\Rotem\\.local\\bin\\uv.exe')
    expect(w.checked).toContain(WINGET_UV)
    expect(w.children).toHaveLength(0)
    expect(w.status()).toBe('JARVIS · not set up · run /jarvis setup')
  })

  test('installs with uv, downloads the model with progress, then starts the helper', async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    w.existing.add(WINGET_UV)
    w.existing.add(NVCUDA)
    w.exists = path => w.existing.has(path) || UV_LOCK.test(path)
    w.onSpawn = child => {
      if (child.argv[0] === WINGET_UV) {
        child.stderr('Resolved 41 packages in 3ms\n')
        child.stderr('Installed 38 packages in 9.2s\n')
        w.existing.add(VENV_PYTHON)
        child.exit(0)
      } else if (child.argv.includes('setup')) {
        child.stdout('{"v":1,"type":"progress","step":"download","pct":42.4,"message":"Downloading large-v3-turbo"}\n')
        child.stderr('huggingface: fetching model.bin\n')
        child.stdout('{"v":1,"type":"progress","step":"done","pct":100,"message":"Ready: large-v3-turbo on cuda"}\n')
        child.exit(0)
      }
    }

    const text = await jarvis($, 'setup large-v3-turbo')
    expect(text).toContain(`Setting up Jarvis with ${WINGET_UV}:`)
    await w.settle(40)

    const [uv, model, helper] = w.children
    const project = uv?.argv[3] ?? ''
    expect(project).toMatch(/[\\/]voice$/)
    expect(uv?.argv).toEqual([
      WINGET_UV, 'sync', '--project', project, '--python', '3.12', '--no-dev', '--no-editable',
      '--reinstall-package', 'jarvis-voice', '--frozen', '--extra', 'cuda',
    ])
    expect(uv?.request.cwd).toBe(project)
    expect(uv?.request.env).toEqual({
      UV_PROJECT_ENVIRONMENT: `${DATA_DIR}\\venv`,
      UV_CACHE_DIR: `${DATA_DIR}\\uv-cache`,
      UV_NO_PROGRESS: '1',
    })
    expect(model?.argv).toEqual([VENV_PYTHON, '-m', 'jarvis_voice', 'setup', '--data-dir', DATA_DIR, '--stt-model', 'large-v3-turbo'])
    expect(model?.request.cwd).toBe(DATA_DIR)
    expect(w.existing.has(`${DATA_DIR}\\README.txt`)).toBe(true)

    expect(w.statuses).toContain('JARVIS · setting up · installing the helper (Python 3.12, with CUDA)')
    expect(w.statuses).toContain('JARVIS · setting up · installing · Installed 38 packages in 9.2s')
    expect(w.statuses).toContain('JARVIS · setting up · Downloading large-v3-turbo 42%')
    expect(w.statuses).toContain('JARVIS · setting up · Ready: large-v3-turbo on cuda')
    expect(w.statuses.some(line => line?.includes('huggingface') === true)).toBe(false)
    expect(w.logs).toContain('Jarvis setup finished (Ready: large-v3-turbo on cuda). Starting the voice helper.')

    // The helper loads the model setup installed, now and in later sessions,
    // until the sttModel setting (auto here) changes.
    expect(w.store.get('sttModel')).toEqual({ model: 'large-v3-turbo', setting: 'auto' })
    expect(helper?.argv).toEqual([VENV_PYTHON, '-m', 'jarvis_voice', 'run', '--data-dir', DATA_DIR, '--stt-model', 'large-v3-turbo'])
    expect(w.status()).toBe('JARVIS · starting')
  })

  test('stops the running helper first; cpu skips CUDA and pins the CPU model; uv from the PATH', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    exitOnShutdown(w)
    w.uvOnPath = 'C:\\tools\\uv\r\nC:\\tools\\uv.exe\r\n'
    w.existing.add(NVCUDA)
    w.onSpawn = child => {
      if (!child.isHelperRun) child.exit(0)
    }
    const text = await jarvis($, 'setup cpu')
    expect(text).toContain('Setting up Jarvis with C:\\tools\\uv.exe:')
    expect(text).toContain('the speech model (small.en)')
    await w.settle(40)
    expect(w.named('shutdown')).toHaveLength(1)
    const uv = w.children.find(child => child.argv[0] === 'C:\\tools\\uv.exe')
    expect(uv?.argv).not.toContain('--extra')
    expect(uv?.argv).not.toContain('--frozen')
    // The helper picks auto's model by the GPU it sees: without CUDA libraries, say the CPU's.
    expect(w.children.find(child => child.argv.includes('setup'))?.argv.slice(-2)).toEqual(['--stt-model', 'small.en'])
    expect(w.store.get('sttModel')).toEqual({ model: 'small.en', setting: 'auto' })
    expect(w.helpers()).toHaveLength(2)
    expect(w.lastHelper().argv.slice(-2)).toEqual(['--stt-model', 'small.en'])
  })

  test('a bare setup follows a changed setting and forgets the old model', { options: { sttModel: 'medium' } }, async ($, on) => {
    const w = world(on, { installed: false })
    w.store.set('sttModel', { model: 'small.en', setting: 'auto' })
    await startSession($, w)
    w.existing.add(WINGET_UV)
    w.onSpawn = child => {
      if (child.argv[0] === WINGET_UV) w.existing.add(VENV_PYTHON)
      if (!child.isHelperRun) child.exit(0)
    }
    expect(await jarvis($, 'setup')).toContain('the speech model (medium)')
    await w.settle(40)
    expect(w.children.find(child => child.argv.includes('setup'))?.argv.slice(-2)).toEqual(['--stt-model', 'medium'])
    expect(w.store.has('sttModel')).toBe(false)
    expect(w.lastHelper().argv.slice(-2)).toEqual(['--stt-model', 'medium'])
  })

  test('setup naming the model the setting already names keeps no override', { options: { sttModel: 'medium' } }, async ($, on) => {
    const w = world(on, { installed: false })
    w.store.set('sttModel', { model: 'small.en', setting: 'medium' })
    await startSession($, w)
    w.existing.add(WINGET_UV)
    w.onSpawn = child => {
      if (child.argv[0] === WINGET_UV) w.existing.add(VENV_PYTHON)
      if (!child.isHelperRun) child.exit(0)
    }
    await jarvis($, 'setup medium')
    await w.settle(40)
    expect(w.store.has('sttModel')).toBe(false)
    expect(w.lastHelper().argv.slice(-2)).toEqual(['--stt-model', 'medium'])
  })

  test('does not run uv over a helper that would not exit', async ($, on) => {
    const w = world(on)
    await startHelper($, w) // answers shutdown, then never exits
    w.existing.add(WINGET_UV)
    await jarvis($, 'setup')
    await w.settle()
    await w.clock.advance(STOP_WAIT_MS * 2)
    await w.settle(40)
    expect(w.children.some(child => child.argv[0] === WINGET_UV)).toBe(false)
    expect(w.logs.some(line => line.startsWith('Jarvis setup did not run: the voice helper did not exit'))).toBe(true)
    expect(w.toasts).toContain('Jarvis setup did not run; the transcript says why.')
    expect(w.helpers()).toHaveLength(2) // tried again; a late retry covers the old one's lock
  })

  test('an error inside setup is reported, not left unhandled', async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    w.existing.add(WINGET_UV)
    w.writeError = 'EPERM: operation not permitted'
    await jarvis($, 'setup')
    await w.settle(40)
    expect(w.children).toHaveLength(0)
    expect(w.logs.some(line => line.startsWith('Jarvis setup failed: '))).toBe(true)
    expect(w.toasts).toContain('Jarvis setup failed; the transcript has the details.')
    expect(w.status()).toBe('JARVIS · not set up · run /jarvis setup')
  })

  test('a failed uv sync is reported and the helper is not set up', async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    w.existing.add(WINGET_UV)
    w.onSpawn = child => {
      child.stderr('error: No interpreter found for Python 3.12 in managed installations\n')
      child.exit(2)
    }
    await jarvis($, 'setup')
    await w.settle(40)
    expect(w.children).toHaveLength(1)
    expect(w.logs).toContain(
      'Jarvis setup failed: uv sync failed (exit 2):\nerror: No interpreter found for Python 3.12 in managed installations',
    )
    expect(w.toasts).toContain('Jarvis setup failed; the transcript has the details.')
    expect(w.status()).toBe('JARVIS · not set up · run /jarvis setup')
  })

  test('a failed model download reports the helper\'s own words', async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    w.existing.add(WINGET_UV)
    w.onSpawn = child => {
      if (child.argv[0] === WINGET_UV) {
        w.existing.add(VENV_PYTHON)
        child.exit(0)
      } else if (child.argv.includes('setup')) {
        child.stdout('{"v":1,"type":"progress","step":"error","pct":30,"message":"Download failed: connection reset"}\n')
        child.exit(1)
      }
    }
    await jarvis($, 'setup small.en')
    await w.settle(40)
    expect(w.statuses).toContain('JARVIS · setting up · Download failed: connection reset')
    expect(w.logs).toContain("Jarvis setup failed: the helper's setup failed (exit 1):\nDownload failed: connection reset")
    expect(w.store.has('sttModel')).toBe(false) // nothing was installed to remember
  })

  test('rejects an unknown option', async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    expect(await jarvis($, 'setup tiny')).toContain('Unknown setup option "tiny"')
    expect(w.children).toHaveLength(0)
  })
})

const LOCAL_PYTHON = `${DATA_DIR}\\local-voice\\venv\\Scripts\\python.exe`

describe('the local voice', () => {
  test('/jarvis setup local installs into its own venv with CUDA torch and downloads the model', async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    w.existing.add(WINGET_UV)
    w.existing.add(NVCUDA)
    w.onSpawn = child => {
      if (child.argv[0] === WINGET_UV && child.argv[1] === 'venv') {
        w.existing.add(LOCAL_PYTHON)
        child.exit(0)
      } else if (child.argv[0] === WINGET_UV) {
        child.stderr('Installed 97 packages in 41s\n')
        child.exit(0)
      } else if (child.argv[0] === LOCAL_PYTHON) {
        child.stdout('{"type":"progress","step":"download","message":"downloading the local voice model (about 3 GB)"}\n')
        child.stderr('Fetching 9 files: 100%\n')
        child.stdout('{"type":"progress","step":"done","message":"local voice ready on cuda: 2.9 s of speech took 1.1 s (voice built-in)"}\n')
        child.exit(0)
      }
    }

    const text = await jarvis($, 'setup local')
    expect(text).toContain(`Setting up the local voice with ${WINGET_UV}:`)
    expect(text).toContain('built-in voice')
    await w.settle(40)

    const [venv, install, download] = w.children
    expect(venv?.argv).toEqual([WINGET_UV, 'venv', `${DATA_DIR}\\local-voice\\venv`, '--python', '3.12', '--allow-existing'])
    const project = install?.argv.at(-1) ?? ''
    expect(project).toMatch(/[\\/]local-voice$/)
    expect(install?.argv).toEqual([
      WINGET_UV, 'pip', 'install', '--python', LOCAL_PYTHON, '--torch-backend', 'cu126',
      '--reinstall-package', 'jarvis-local-voice', project,
    ])
    expect(install?.request.env?.UV_CACHE_DIR).toBe(`${DATA_DIR}\\uv-cache`)
    expect(download?.argv).toEqual([LOCAL_PYTHON, '-m', 'jarvis_local_voice', 'download', '--models-dir', `${DATA_DIR}\\models`, '--verify'])
    expect(w.statuses).toContain('JARVIS · setting up · installing the local voice (PyTorch with CUDA, about 3 GB) · Installed 97 packages in 41s')
    expect(w.statuses).toContain('JARVIS · setting up · local voice ready on cuda: 2.9 s of speech took 1.1 s (voice built-in)')
    expect(w.logs).toContain(
      'Jarvis local voice installed (local voice ready on cuda: 2.9 s of speech took 1.1 s (voice built-in)). Switch to it with /jarvis engine local.',
    )
    expect(w.store.has('voiceEngine')).toBe(false) // installing does not switch
  })

  test('/jarvis setup local cpu installs CPU torch and passes the reference clip', { options: { localVoiceClip: 'C:\\Voices\\jarvis.mp3' } }, async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    w.existing.add(WINGET_UV)
    w.onSpawn = child => child.exit(0)
    const text = await jarvis($, 'setup local cpu')
    expect(text).toContain('Voice: copied from C:\\Voices\\jarvis.mp3.')
    await w.settle(40)
    expect(w.children[1]?.argv).toContain('cpu')
    expect(w.children[2]?.argv.slice(-2)).toEqual(['--voice', 'C:\\Voices\\jarvis.mp3'])
  })

  test('a failed install is reported and nothing switches', async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    w.existing.add(WINGET_UV)
    w.onSpawn = child => {
      if (child.argv[1] === 'pip') child.stderr('error: No solution found when resolving dependencies\n')
      child.exit(child.argv[1] === 'pip' ? 1 : 0)
    }
    await jarvis($, 'setup local')
    await w.settle(40)
    expect(w.children).toHaveLength(2)
    expect(w.logs.at(-1)).toContain(`Jarvis local voice setup failed: ${WINGET_UV} pip install failed (exit 1):`)
    expect(w.logs.at(-1)).toContain('No solution found')
  })

  test('/jarvis engine local needs the local voice installed', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(await jarvis($, 'engine')).toBe('Voice engine: Fish Audio. Switch with /jarvis engine fish or /jarvis engine local.')
    expect(await jarvis($, 'engine local')).toBe(
      'The local voice is not installed yet. Run /jarvis setup local first (it downloads about 6 GB).',
    )
    expect(await jarvis($, 'engine kokoro')).toBe('Unknown engine "kokoro". Use /jarvis engine fish or /jarvis engine local.')
    expect(w.store.has('voiceEngine')).toBe(false)
  })

  test('/jarvis engine local restarts the helper on the local voice, and fish switches back', { options: { localVoiceClip: 'C:\\Voices\\jarvis.mp3' } }, async ($, on) => {
    const w = world(on)
    const first = await startHelper($, w)
    expect(first.request.env?.JARVIS_TTS_ENGINE).toBeUndefined()
    w.existing.add(LOCAL_PYTHON)
    w.respond = command => {
      if (command.name === 'shutdown') w.lastHelper().exit(0)
      return { status: 200, body: { ok: true } }
    }
    expect(await jarvis($, 'engine local')).toBe(
      'Switched to the local voice. The helper restarts and loads the model onto your GPU, which takes a few seconds.',
    )
    await w.settle(20)
    expect(w.store.get('voiceEngine')).toBe('local')
    expect(w.helpers()).toHaveLength(2)
    expect(w.lastHelper().request.env?.JARVIS_TTS_ENGINE).toBe('local')
    expect(w.lastHelper().request.env?.JARVIS_LOCAL_VOICE).toBe('C:\\Voices\\jarvis.mp3')
    expect(await jarvis($, 'engine')).toBe('Voice engine: the local voice (Chatterbox). Switch with /jarvis engine fish or /jarvis engine local.')

    w.lastHelper().hello()
    await w.settle()
    expect(await jarvis($, 'engine fish')).toBe('Switched to Fish Audio. The helper restarts.')
    await w.settle(20)
    expect(w.helpers()).toHaveLength(3)
    expect(w.lastHelper().request.env?.JARVIS_TTS_ENGINE).toBeUndefined()
  })
})

describe('hands-free', () => {
  test('the helper starts with the wake word on and talking over Jarvis interrupting him', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(w.named('config')[0]?.body).toEqual({
      pttKey: 'right ctrl',
      language: 'en',
      wakeWord: true,
      bargeIn: 'speech',
      wakeThreshold: 0.5,
    })
  })

  test('status says when echo cancelling could not start, until a new helper tries again', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w)
    helper.event({
      type: 'error',
      code: 'aec_unavailable',
      message: 'Echo cancelling could not be started: blocked',
      hint: 'Run /jarvis setup to reinstall the voice helper.',
      fatal: false,
    })
    await w.settle()
    expect(w.toasts.at(-1)).toBe('Jarvis: Echo cancelling could not be started: blocked (Run /jarvis setup to reinstall the voice helper.)')
    expect(await jarvis($, '')).toContain('talking over Jarvis interrupts him · echo cancelling unavailable')
    helper.hello()
    await w.settle()
    expect(await jarvis($, '')).toContain('talking over Jarvis interrupts him · echo cancelling on')
  })

  test('status shows echo cancelling switched off', { options: { echoCancelling: 'off' } }, async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(await jarvis($, '')).toContain('talking over Jarvis interrupts him · echo cancelling off')
  })

  test('the settings shape the first config', { options: { wakeWord: 'off', bargeIn: 'wake', wakeSensitivity: 'high' } }, async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(w.named('config')[0]?.body).toMatchObject({ wakeWord: false, bargeIn: 'wake', wakeThreshold: 0.3 })
  })

  test('ready with the wake phrase: the status line and the toast say it; awake reads as keep talking', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w) // the first ready came without the phrase (the model was loading)
    expect(w.toasts).toEqual(['Jarvis is ready. Hold right ctrl to talk.'])
    helper.event({ type: 'ready', sttModel: 'large-v3-turbo', sttDevice: 'cuda', pttKey: 'right ctrl', wakePhrase: 'Hey Jarvis', bargeIn: 'speech' })
    await w.settle()
    expect(w.status()).toBe('JARVIS · ready · say "Hey Jarvis" or hold right ctrl to talk')
    expect(w.toasts.at(-1)).toBe('Jarvis is listening for "Hey Jarvis".')
    helper.event({ type: 'state', state: 'awake' })
    await w.settle()
    expect(w.status()).toBe('JARVIS · awake · keep talking')
    expect(await jarvis($, 'wake')).toBe('Say "Hey Jarvis" · talking over Jarvis interrupts him. Switch with /jarvis wake on, jarvis or off.')
  })

  test('/jarvis wake off and on tell the running helper and are remembered', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(await jarvis($, 'wake off')).toBe('Wake word off: hold the push-to-talk key to talk.')
    expect(w.store.get('wakeWord')).toBe('off')
    expect(w.named('config').at(-1)?.body).toEqual({ wakeWord: false })
    expect(await jarvis($, 'wake on')).toBe('Jarvis listens for "Hey Jarvis". Nothing is recorded or sent until he hears it.')
    expect(w.named('config').at(-1)?.body).toEqual({ wakeWord: true })
    expect(await jarvis($, 'wake maybe')).toBe('Unknown choice "maybe". Use /jarvis wake on, jarvis or off.')
  })

  test('a saved choice reaches the helper when it starts (on and off were saved as true and false)', async ($, on) => {
    const w = world(on)
    w.store.set('wakeWord', false)
    w.store.set('bargeIn', 'off')
    await startHelper($, w)
    expect(w.named('config')[0]?.body).toMatchObject({ wakeWord: false, bargeIn: 'off' })
  })

  test('/jarvis wake jarvis switches plain "Jarvis" on and back off, and is remembered', async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w, PLAIN_CAPABLE)
    expect(await jarvis($, 'wake jarvis')).toBe(
      'Jarvis listens for "Jarvis" at the start of what you say, and for "Hey Jarvis". Nothing is recorded or sent until he hears one; plain "Jarvis" never interrupts him.',
    )
    expect(w.store.get('wakeWord')).toBe('jarvis')
    expect(w.named('config').at(-1)?.body).toEqual({ wakeWord: true, plainWake: true })

    // The helper announces the new phrase: the status line and /jarvis wake show it.
    helper.event({ type: 'ready', sttModel: 'large-v3-turbo', sttDevice: 'cuda', pttKey: 'right ctrl', wakePhrase: 'Jarvis', bargeIn: 'speech' })
    await w.settle()
    expect(w.status()).toBe('JARVIS · ready · say "Jarvis" or hold right ctrl to talk')
    expect(await jarvis($, 'wake')).toBe(
      'Say "Jarvis" or "Hey Jarvis" · talking over Jarvis interrupts him. Switch with /jarvis wake on, jarvis or off.',
    )
    expect(await jarvis($, '')).toContain('Say "Jarvis" or "Hey Jarvis"')

    expect(await jarvis($, 'wake on')).toBe('Jarvis listens for "Hey Jarvis". Nothing is recorded or sent until he hears it.')
    expect(w.named('config').at(-1)?.body).toEqual({ wakeWord: true, plainWake: false })
    expect(await jarvis($, 'wake off')).toBe('Wake word off: hold the push-to-talk key to talk.')
    expect(w.named('config').at(-1)?.body).toEqual({ wakeWord: false, plainWake: false })
    expect(w.store.get('wakeWord')).toBe('off')
  })

  test('a saved plain "Jarvis" reaches a helper that takes it when it starts', async ($, on) => {
    const w = world(on)
    w.store.set('wakeWord', 'jarvis')
    await startHelper($, w, PLAIN_CAPABLE)
    expect(w.named('config')[0]?.body).toEqual({
      pttKey: 'right ctrl',
      language: 'en',
      wakeWord: true,
      plainWake: true,
      bargeIn: 'speech',
      wakeThreshold: 0.5,
    })
  })

  test('an older helper is not sent plainWake, and /jarvis wake jarvis says to update it', { options: { wakeWord: 'jarvis' } }, async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w) // hello without wake.plain: it would refuse the whole config
    expect(w.named('config')[0]?.body).toEqual({ pttKey: 'right ctrl', language: 'en', wakeWord: true, bargeIn: 'speech', wakeThreshold: 0.5 })
    expect(await jarvis($, 'wake jarvis')).toBe(
      'Saved, but this voice helper is older than plain "Jarvis": run /jarvis setup to update it. Until then, say "Hey Jarvis".',
    )
    expect(w.named('config').at(-1)?.body).toEqual({ wakeWord: true })
    helper.event({ type: 'ready', sttModel: 'large-v3-turbo', sttDevice: 'cuda', pttKey: 'right ctrl', wakePhrase: 'Hey Jarvis' })
    await w.settle()
    expect(await jarvis($, 'wake')).toContain('Say "Hey Jarvis" (plain "Jarvis" is not ready: run /jarvis setup)')
  })

  test('/jarvis wake says plain "Jarvis" is on its way while its model downloads', { options: { wakeWord: 'jarvis' } }, async ($, on) => {
    const w = world(on)
    const helper = await startHelper($, w, PLAIN_CAPABLE)
    helper.event({ type: 'ready', sttModel: 'large-v3-turbo', sttDevice: 'cuda', pttKey: 'right ctrl', wakePhrase: 'Hey Jarvis', bargeIn: 'speech' })
    await w.settle()
    expect(await jarvis($, 'wake')).toBe(
      'Say "Hey Jarvis" (plain "Jarvis" is not ready yet: its model is downloading; if it stays so, run /jarvis setup) · talking over Jarvis interrupts him. Switch with /jarvis wake on, jarvis or off.',
    )
    helper.event({ type: 'ready', sttModel: 'large-v3-turbo', sttDevice: 'cuda', pttKey: 'right ctrl', wakePhrase: 'Jarvis', bargeIn: 'speech' })
    await w.settle()
    expect(await jarvis($, 'wake')).toContain('Say "Jarvis" or "Hey Jarvis"')
  })

  test('the wakeWord setting can choose plain "Jarvis"', { options: { wakeWord: 'jarvis' } }, async ($, on) => {
    const w = world(on)
    await startHelper($, w, PLAIN_CAPABLE)
    expect(w.named('config')[0]?.body).toMatchObject({ wakeWord: true, plainWake: true })
  })

  test('/jarvis bargein picks what interrupts Jarvis', async ($, on) => {
    const w = world(on)
    await startHelper($, w)
    expect(await jarvis($, 'bargein wake')).toBe('Now only "Hey Jarvis" interrupts him.')
    expect(w.store.get('bargeIn')).toBe('wake')
    expect(w.named('config').at(-1)?.body).toEqual({ bargeIn: 'wake' })
    expect(await jarvis($, 'bargein speech')).toContain('use /jarvis bargein wake there')
    expect(await jarvis($, 'bargein')).toBe('Now talking over Jarvis interrupts him. Choose with /jarvis bargein speech, wake or off.')
    expect(await jarvis($, 'bargein loud')).toBe('Unknown choice "loud". Use /jarvis bargein speech, wake or off.')
  })

  test('without a running helper the choice is saved for later', async ($, on) => {
    const w = world(on, { installed: false })
    await startSession($, w)
    expect(await jarvis($, 'wake off')).toBe('Wake word off: hold the push-to-talk key to talk.')
    expect(w.store.get('wakeWord')).toBe('off')
    expect(w.commands).toHaveLength(0)
  })
})
