// The jarvis mod's $.state contract: values the host keeps for the session
// (they survive a hot reload of the module; the module's variables do not).

/** What the status line and the band show; one value, redrawn on change. */
export type JarvisPhase =
  /** Not a local session (cloud, -p): the helper never starts here. */
  | 'unavailable'
  /** Not started yet. */
  | 'stopped'
  /** The helper's venv is missing: /jarvis setup installs it. */
  | 'not_installed'
  /** /jarvis setup is running. */
  | 'setup'
  /** The helper's own states (protocol HelperState). */
  | 'starting'
  | 'sleeping'
  | 'listening'
  | 'transcribing'
  | 'speaking'
  | 'awake'
  | 'error'
  /** Waiting out a restart backoff. */
  | 'restarting'
  /** Another Claude Code window owns the helper (single instance). */
  | 'elsewhere'
  /** Gave up restarting; /jarvis restart tries again. */
  | 'failed'

export type JarvisView = {
  phase: JarvisPhase
  /** A hint, an error message or the setup step, shown after the phase. */
  detail?: string
  /** The push-to-talk key as the helper reported it ("right ctrl"). */
  pttKey?: string
  /** The wake phrase ("Hey Jarvis") while the helper listens for it. */
  wakePhrase?: string
  /** The last transcribed utterance. */
  lastUtterance?: string
  /** Microphone level 0..1, updated a few times a second while listening. */
  micLevel?: number
}

/**
 * The running helper's control address. Kept in session state so a reloaded
 * module can ask an orphaned helper (killed parent, live grandchild on
 * Windows) to shut down before it starts a new one.
 */
export type JarvisHelperRef = {
  port: number
  token: string
  pid: number
}

declare module 'claude-code' {
  interface PluginState {
    jarvis: {
      view: JarvisView
      helper: JarvisHelperRef | null
    }
  }
}
