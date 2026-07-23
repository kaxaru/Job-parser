/* Store (ViewModel) — единый источник UI-состояния + подписка.
   Поток однонаправленный: событие -> store.update(patch) -> notify -> View.render(state).
   View не мутирует данные напрямую, только читает state и вызывает команды. */

export function createStore(initial) {
  let state = initial;
  const subs = new Set();
  return {
    get: () => state,
    /** Слить patch в состояние и (по умолчанию) уведомить подписчиков (View).
        notify=false — тихое обновление без ре-рендера (напр. тоггл статуса карточки,
        который точечно перекрашивается, чтобы не перерисовывать 10k карточек). */
    update(patch, notify = true) {
      state = { ...state, ...patch };
      if (notify) subs.forEach(fn => { fn(state); });
    },
    /** Подписать View; возвращает функцию отписки. */
    subscribe(fn) {
      subs.add(fn);
      return () => subs.delete(fn);
    },
  };
}
