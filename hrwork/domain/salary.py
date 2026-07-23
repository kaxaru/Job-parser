"""Value Object зарплаты. Единый источник gross→net (НДФЛ 13%) и конвертации в RUB
(раньше — parsing._salary_mid + infrastructure.net.rates.to_rub над примитивами)."""
from dataclasses import dataclass

from hrwork.config import NET_FROM_GROSS


@dataclass(frozen=True)
class Salary:
    frm: int | None
    to: int | None
    currency: str | None = None
    gross: bool = False

    @classmethod
    def from_raw(cls, sal: dict | None) -> "Salary | None":
        """{from,to,currency,gross} -> Salary (или None, если вилки нет)."""
        if not sal or (sal.get("from") is None and sal.get("to") is None):
            return None                          # is None, не truthiness: вилка from=0 — валидна
        return cls(sal.get("from"), sal.get("to"), sal.get("currency"), bool(sal.get("gross")))

    @property
    def mid(self) -> int | None:
        if self.frm is not None and self.to is not None:
            return (self.frm + self.to) // 2
        return self.frm if self.frm is not None else self.to

    def net(self) -> "Salary":
        """gross -> net (−13% НДФЛ). Если уже net — возвращает себя."""
        if not self.gross:
            return self
        f = int(self.frm * NET_FROM_GROSS) if self.frm else None
        t = int(self.to * NET_FROM_GROSS) if self.to else None
        return Salary(f, t, self.currency, gross=False)

    def net_triple(self) -> tuple[int | None, int | None, int | None]:
        """(from, to, mid) уже net — для заполнения примитивных полей Vacancy (совместимость)."""
        n = self.net()
        return n.frm, n.to, n.mid
