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

const { setStale, statusBadge, accountBadge, setAccounts, salLine, subLine } =
  await import('../../src/feed/view.js');

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


/* ── Баннер «данные устарели» ──
   Заглушка `document` выше отдаёт НОВЫЙ объект на каждый getElementById, поэтому записанное
   в него прочитать нельзя — на время теста подменяем геттер на свой узел. */
describe('setStale — баннер возраста среза', () => {
  const withBanner = fn => {
    const el = { hidden: false, textContent: '' };
    const prev = document.getElementById;
    document.getElementById = id => (id === 'stale-banner' ? el : prev(id));
    try { fn(el); } finally { document.getElementById = prev; }
  };
  const at = new Date('2026-08-16T17:53:00Z');

  it('без повода баннер гаснет и пустеет', () => {
    withBanner(el => {
      setStale(null);
      assert.equal(el.hidden, true);
      assert.equal(el.textContent, '');
    });
  });

  /* Гаснуть баннер обязан САМ: его перерисовывает пятиминутный тик, и после удачного сбора
     вкладка должна перестать пугать без F5. */
  it('после свежего сбора зажжённый баннер гаснет', () => {
    withBanner(el => {
      setStale({ hours: 79, at });
      setStale(null);
      assert.equal(el.hidden, true);
    });
  });

  it('до двух суток возраст показывается в часах', () => {
    withBanner(el => {
      setStale({ hours: 46.4, at });
      assert.equal(el.hidden, false);
      assert.match(el.textContent, /^⚠ Данные устарели/);
      assert.match(el.textContent, /это 46 ч назад/);
    });
  });

  it('дальше двух суток — в днях: «79 ч» глазом не читается', () => {
    withBanner(el => {
      setStale({ hours: 79, at });
      assert.match(el.textContent, /это 3 дн назад/);
    });
  });

  it('текст называет, куда смотреть', () => {
    withBanner(el => {
      setStale({ hours: 79, at });
      assert.match(el.textContent, /cron_collect\.log/);
      assert.match(el.textContent, /hh\.py collect/);
    });
  });
});


describe('accountBadge — метка профиля отклика (RFC-004)', () => {
  it('при одном аккаунте метки нет', () => {
    setAccounts([{ code: 'main', label: 'основной' }]);
    assert.equal(accountBadge(['main']), '');
  });
  it('при двух аккаунтах показывает метку профиля', () => {
    setAccounts([{ code: 'main', label: 'основной' }, { code: 'acc2', label: 'Резюме B' }]);
    const html = accountBadge(['acc2']);
    assert.match(html, /Резюме B/);
    assert.match(html, /👤/);
  });
  it('два аккаунта на одной вакансии — конфликт, оба в метке', () => {
    setAccounts([{ code: 'main', label: 'основной' }, { code: 'acc2', label: 'Резюме B' }]);
    const html = accountBadge(['acc2', 'main']);
    assert.match(html, /основной \+ Резюме B|Резюме B \+ основной/);
    assert.match(html, /⚠/);
  });
  it('statusBadge вклеивает метку профиля applied.accounts', () => {
    setAccounts([{ code: 'main', label: 'основной' }, { code: 'acc2', label: 'Резюме B' }]);
    const html = statusBadge({ applied: { accounts: ['acc2'] } });
    assert.match(html, /Резюме B/);
    setAccounts([{ code: 'main', label: 'основной' }]);   /* вернуть один аккаунт для прочих тестов */
  });
});


/* ── Общие строки карточки и модалки (аудит 2026-09-22, §3.3-32) ──
   Собирались ДВАЖДЫ (`cardHTML` / `showModal`) и по-разному; теперь один билдер на обе
   поверхности. Ожидаемое — литералы по спеке: подписи берутся из офлайн-фолбэков
   `SCHED_LABELS`/`EMP_LABELS` (мост в этом процессе не инжектится), разряды суммы
   разделяет `toLocaleString('ru')` неразрывным пробелом (U+00A0, как его печатает Node). */
describe('subLine / salLine — общие строки двух поверхностей', () => {
  it('subLine: «работодатель · город», чужой текст экранирован', () => {
    assert.equal(subLine({ employer: 'ООО "Ромашка"', city: 'Москва' }),
      'ООО &quot;Ромашка&quot; · Москва');
  });

  it('subLine: пустые части отброшены, разделитель не висит', () => {
    assert.equal(subLine({ employer: 'Acme', city: '' }), 'Acme');
    assert.equal(subLine({ employer: '', city: '' }), '');
  });

  it('salLine (карточка): «зарплата · грейд», без формата и оформления', () => {
    /* `fmtSal` отбивает сумму неразрывным пробелом (U+00A0), как и разряды `toLocaleString('ru')`. */
    assert.equal(salLine({ sal_from: 100000, currency: 'RUR', exp: '1–3 года' }),
      'от\u00A0100\u00A0000\u00A0₽/мес · 1–3 года');
  });

  it('salLine extended (модалка): добавляет формат работы и оформление', () => {
    assert.equal(
      salLine({ sal_from: 100000, currency: 'RUR', exp: '1–3 года',
                schedule: 'remote', emp_ids: ['labor_code'] }, true),
      'от\u00A0100\u00A0000\u00A0₽/мес · 1–3 года · Удалённо · ТК РФ/РБ');
  });

  it('salLine: без суммы строка начинается с грейда, висящего разделителя нет', () => {
    assert.equal(salLine({ exp: '3–6 лет' }), '3–6 лет');
    assert.equal(salLine({ exp: '' }), '');
  });
});
