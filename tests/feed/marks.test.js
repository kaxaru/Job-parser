/* Тесты marks-сервиса (src/feed/marks.js): pushServer обязан бросать и на
   HTTP-ошибке — fetch реджектится только на сетевом сбое, ответ 500 без
   проверки статуса давал бы ложный индикатор «✓ синхронизировано». */
import assert from 'node:assert/strict';
import { afterEach, describe, it } from 'node:test';

import { applyVacancy, loadDescriptions, pushServer } from '../../src/feed/marks.js';

const realFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = realFetch; });

describe('pushServer — POST отметок на сервер', () => {
  it('резолвится при 2xx (сервер отвечает 204)', async () => {
    globalThis.fetch = async () => ({ ok: true, status: 204 });
    await pushServer({ 1: 'applied' });
  });

  it('бросает при HTTP-ошибке', async () => {
    globalThis.fetch = async () => ({ ok: false, status: 500 });
    await assert.rejects(() => pushServer({}), /HTTP 500/);
  });

  it('пробрасывает сетевой сбой', async () => {
    globalThis.fetch = async () => { throw new TypeError('network down'); };
    await assert.rejects(() => pushServer({}), /network down/);
  });
});

/* Регрессия 01.08.2026: карточка-призрак (отклик на выпавшую из выдачи вакансию) не
   находилась поиском по компании — журнал получал пустого работодателя, потому что
   прямой клик в ленте его не отправлял. Работодатель фиксируется В МОМЕНТ КЛИКА:
   к моменту дренажа очереди спросить его будет уже не у кого. */
describe('applyVacancy — POST отклика в фоне', () => {
  function captureBody() {
    const sent = {};
    globalThis.fetch = async (_url, opts) => {
      sent.body = JSON.parse(opts.body);
      return { ok: true, status: 200, json: async () => ({ status: 'applied', letter: false }) };
    };
    return sent;
  }

  it('кладёт работодателя в тело запроса рядом с названием', async () => {
    const sent = captureBody();
    await applyVacancy('77', 'https://hh.ru/vacancy/77', 'письмо', 'Python Developer', 'Acme');
    assert.deepEqual(sent.body, {
      id: '77', url: 'https://hh.ru/vacancy/77', cover: 'письмо',
      name: 'Python Developer', employer: 'Acme',
    });
  });

  it('без работодателя шлёт пустую строку', async () => {
    const sent = captureBody();
    await applyVacancy('77', 'u', '', 'n');
    assert.equal(sent.body.employer, '');
  });
});

/* Регрессия 08.08.2026: отвергнутый промис загрузки описаний кешировался НАВСЕГДА
   (`if (_descPromise) return _descPromise`), поэтому один обрыв при первом открытии
   модалки блокировал описания до F5 — каждая следующая карточка показывала
   «Не удалось загрузить описание», хотя сеть уже вернулась.

   Заглушка DOM: loadDescriptions грузит feed-desc.js тегом <script> (а не fetch — иначе
   не работало бы из file://), поэтому подменяем createElement/head и дёргаем onload/onerror. */
describe('loadDescriptions — ленивая загрузка описаний', () => {
  function stubDom(outcome) {
    const scripts = [];
    globalThis.window = {};
    globalThis.document = {
      createElement: () => {
        const s = {};
        scripts.push(s);
        return s;
      },
      head: {
        appendChild: s => {
          if (outcome() === 'ok') { globalThis.window.__DESC = { 1: '<p>ок</p>' }; s.onload(); }
          else { s.onerror(); }
        },
      },
    };
    return scripts;
  }

  it('после сбоя следующая попытка грузит заново и отдаёт описания', async () => {
    let mode = 'fail';
    stubDom(() => mode);
    await assert.rejects(() => loadDescriptions(), /feed-desc\.js не загружен/);
    mode = 'ok';
    assert.deepEqual(await loadDescriptions(), { 1: '<p>ок</p>' });
    delete globalThis.window;
    delete globalThis.document;
  });
});
