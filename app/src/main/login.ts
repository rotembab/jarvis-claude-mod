// Start with Windows: what main.ts hands app.setLoginItemSettings (and
// getLoginItemSettings, which must get the same to find the entry). Windows
// then runs `"<electron.exe>" "<app folder>"` at sign-in. Electron quotes
// each argument itself (AddQuoteForArg in Electron 44), so the folder goes in
// as it is: quoted here, it would be quoted twice and Electron would not find
// the app at sign-in.

export type LoginItem = { path: string; args: string[] }

export function loginItem(execPath: string, appPath: string): LoginItem {
  return { path: execPath, args: [appPath] }
}
