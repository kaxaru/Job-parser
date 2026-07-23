/* Тесты marks-сервиса (src/feed/marks.js): pushServer обязан бросать и на
   HTTP-ошибке — fetch реджектится только на сетевом сбое, ответ 500 без
   проверки статуса давал бы ложный индикатор «✓ синхронизировано». */
import assert from 'node:assert/strict';
import { afterEach, describe, it } from 'node:test';

import { pushServer } from '../../src/feed/marks.js';

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
