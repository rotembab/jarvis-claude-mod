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
  /** The wake phrase while the helper listens for it: "Hey Jarvis", or "Jarvis" when plain "Jarvis" works too. */
  wakePhrase?: string
  /** The last transcribed utterance. */
  lastUtterance?: string
  /** Microphone level 0..1, updated a few times a second while listening. */
  micLevel?: number
}

/** One line of the HUD's action log: a tool call Claude made. */
export type HudAction = {
  id: number
  /** The tool and what it worked on ("Bash npm test"). */
  label: string
  status: 'running' | 'done' | 'failed'
}

/** What the HUD pane's texts show beside the view. */
export type JarvisHud = {
  /** Claude is working on a turn. */
  isThinking: boolean
  /** The latest tool calls, newest first. */
  actions: HudAction[]
  /** The text of Claude's last reply, shown under the ring in focus mode. */
  lastReply?: string
  /** Focus mode shows: the pane asks for most of the screen and shows the reply. */
  isFocus?: boolean
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
      hud: JarvisHud
      /** Focus mode folds the conversation's rows away (the HUD docked beside them fills the screen). */
      folded: boolean
    }
  }
}
