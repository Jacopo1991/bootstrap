// Fixture mod: calls nothing but command.register; /modping prints a fixed token.
export function register(on) {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'modping', description: 'CI fixture' })
    return next(e)
  })
  on('command.run', { command: 'modping' }, async () => {
    return { text: 'MODLOADED-7f3a' }
  })
}
