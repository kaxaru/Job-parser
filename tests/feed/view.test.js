/* Тесты рендера бейджей (src/feed/view.js). View — единственный модуль ленты, который
   клеит чужой текст в HTML-атрибуты, поэтому его экранирование стоит проверять отдельно.

   view.js трогает DOM ещё на импорте (модалка ищет свои узлы и вешает Tab-ловушку), а в
   node:test документа нет — подставляем минимальную заглушку ДО динамического import. */
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

const stubEl = () => ({
  addEventListener() {}, querySelector: () => null, querySelectorAll: () => [],
  classList: { add() {}, remove() {}, toggle() {} }, style: {}, innerHTML: '',
  focus() {}, after() {}, setAttribute() {},
});
globalThis.document = {
  getElementById: stubEl, querySelector: () => null, querySelectorAll: () => [],
  createElement: stubEl, documentElement: stubEl(), body: stubEl(), head: stubEl(),
};
globalThis.window = { matchMedia: () => ({ matches: false }) };

const { statusBadge } = await import('../../src/feed/view.js');

const withChat = chat => ({
  id: '1', name: 'x', techs: [], status: null, needs_form: false, form_dead: false, chat,
});

/* Вывод сравнивается ЦЕЛИКОМ, а не подстрокой: `includes('title="…"')` ничего не говорил
   про остальную разметку, и вторая, НЕэкранированная копия чужого текста в теле бейджа
   тест бы не уронила — то есть единственный XSS-тест фронта дыру не закрывал
   (аудит 09.08.2026). Разметка полностью детерминирована: статуса HH нет (status: null),
   формы нет, контакта и отклика нет, метки времени у чата нет -> ни возраста, ни замка. */
describe('statusBadge — превью чата в подсказке бейджа', () => {
  /* Регрессия 08.08.2026: превью сперва вручную гнали через replace(/"/g,'&quot;'), а
     потом ещё раз через esc() при подстановке в title — в подсказке пользователь видел
     «&quot;Здравствуйте&quot;» вместо кавычек. Экранирование ровно одно. */
  it('кавычка экранируется РОВНО один раз', () => {
    const html = statusBadge(withChat({
      needs_reply: true, sender: 'human', label: 'ответ', preview: 'Скажите "да"',
    }));
    assert.equal(html, '<span class="status-badge" style="background:#D53F4B"'
                     + ' title="Скажите &quot;да&quot;">👤 ответ</span>');
  });

  it('угловые скобки и амперсанд из чужого текста экранируются', () => {
    const html = statusBadge(withChat({
      needs_reply: true, sender: 'human', label: 'ответ', preview: '<b>R&D</b>',
    }));
    assert.equal(html, '<span class="status-badge" style="background:#D53F4B"'
                     + ' title="&lt;b&gt;R&amp;D&lt;/b&gt;">👤 ответ</span>');
  });

  it('превью нет -> подсказка пустая, не «undefined»', () => {
    const html = statusBadge(withChat({ needs_reply: true, sender: 'bot', label: 'ответ' }));
    assert.equal(html, '<span class="status-badge" style="background:#6C7885"'
                     + ' title="">🤖 ответ</span>');
  });
});
