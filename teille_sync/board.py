"""The GitHub Project, as four operations.

Projects v2 offers no transaction, so claiming is optimistic: write, then
read back, and let the loser of a race skip rather than duplicate. That
is enough because the cost of losing is one document deferred, and the
cost of not checking is two machines converting the same volume.

The id file is the contract. A field renamed or recreated in the UI gets
a new id, so every lookup here is by name against the ids loaded at
startup, and a name that is missing raises rather than writing nowhere.
"""

from dataclasses import dataclass
from datetime import datetime

SET_FIELD = """
mutation($p:ID!,$i:ID!,$f:ID!,$value:ProjectV2FieldValue!){
  updateProjectV2ItemFieldValue(input:{
    projectId:$p,itemId:$i,fieldId:$f,value:$value}){ projectV2Item{ id } } }
"""

READ_ITEM = """
query($i:ID!){ node(id:$i){ ... on ProjectV2Item{
  fieldValues(first:20){ nodes{
    ... on ProjectV2ItemFieldTextValue{ text field{
      ... on ProjectV2FieldCommon{ name } } } } } } } }
"""

PENDING = """
query($p:ID!,$c:String){ node(id:$p){ ... on ProjectV2{
  items(first:100, after:$c){ pageInfo{ hasNextPage endCursor }
    nodes{ id content{ ... on DraftIssue{ title } }
      fieldValues(first:20){ nodes{
        ... on ProjectV2ItemFieldSingleSelectValue{ name field{
          ... on ProjectV2FieldCommon{ name } } }
        ... on ProjectV2ItemFieldTextValue{ text field{
          ... on ProjectV2FieldCommon{ name } } } } } } } } } }
"""

TODO, WIP = "À traiter", "En cours"


@dataclass(frozen=True, slots=True)
class Card:
    identifier: str
    item_id: str
    status: str
    detail: str


class Board:
    def __init__(self, ids, transport):
        self.ids = ids
        self.send = transport
        self.project = ids["project"]["id"]
        self.fields = ids["fields"]

    # -- writing ---------------------------------------------------------

    def _field(self, name):
        try:
            return self.fields[name]
        except KeyError:
            raise KeyError(
                f"the board has no field named {name!r} — the id file is "
                f"stale, run `teille-sync ids refresh`") from None

    def _set(self, item_id, field_name, value):
        self.send(SET_FIELD, {"p": self.project, "i": item_id,
                              "f": self._field(field_name)["id"],
                              "value": value})

    def _select(self, item_id, field_name, label):
        """Write a single-select. A label the board has no option for is
        skipped: the pipeline emits steps the board never modelled, and
        inventing an option would be a guess."""
        options = self._field(field_name).get("options", {})
        if label is None or label not in options:
            return False
        self._set(item_id, field_name, {"singleSelectOptionId": options[label]})
        return True

    def claim(self, card, machine, now):
        """Take a card. True if it is ours after the read-back."""
        stamp = f"{machine} · {now.isoformat(timespec='seconds')}"
        self._select(card.item_id, "Status", WIP)
        self._set(card.item_id, "Détail", {"text": stamp})
        answer = self.send(READ_ITEM, {"i": card.item_id})
        nodes = (((answer.get("node") or {}).get("fieldValues") or {})
                 .get("nodes") or [])
        for node in nodes:
            if (node.get("field") or {}).get("name") == "Détail":
                return node.get("text") == stamp
        return False

    def release(self, card):
        """Put a claimed card back, and clear the claim with it. An empty
        Détail is what makes a released card indistinguishable from one
        never taken."""
        self._select(card.item_id, "Status", TODO)
        self._set(card.item_id, "Détail", {"text": ""})

    def write(self, card, verdict, pages, version, now):
        if card.identifier not in self.ids.get("items", {}):
            raise KeyError(f"{card.identifier} has no card on the board")
        self._select(card.item_id, "Status", verdict.status)
        self._select(card.item_id, "Cause", verdict.cause)
        self._select(card.item_id, "Phase", verdict.phase)
        self._set(card.item_id, "Détail", {"text": verdict.detail[:900]})
        self._set(card.item_id, "Pertes", {"number": verdict.losses})
        self._set(card.item_id, "Pages", {"number": pages})
        self._set(card.item_id, "Date de traitement",
                  {"date": now.date().isoformat()})
        self._set(card.item_id, "Version pipeline", {"text": version})

    # -- reading -----------------------------------------------------------

    def _card_from_node(self, node):
        """One `PENDING` item node, as a `Card`. Board field order is not
        guaranteed by the API, so this reads by field name rather than
        position."""
        identifier = ((node.get("content") or {}).get("title")) or ""
        status, detail = "", ""
        for value in (node.get("fieldValues") or {}).get("nodes") or []:
            name = (value.get("field") or {}).get("name")
            if name == "Status":
                status = value.get("name", "")
            elif name == "Détail":
                detail = value.get("text", "")
        return Card(identifier=identifier, item_id=node["id"],
                    status=status, detail=detail)

    def _all_cards(self):
        """Every card on the board, paging through `PENDING` to the end.

        `items(first:100)` never returns the whole board in one page — it
        holds 396 cards — so this keeps asking as long as
        `pageInfo.hasNextPage` says there is more, passing back
        `endCursor` as the next request's cursor. Stopping after the
        first page would silently drop three quarters of the corpus.
        """
        cursor = None
        cards = []
        while True:
            answer = self.send(PENDING, {"p": self.project, "c": cursor})
            items = ((answer.get("node") or {}).get("items")) or {}
            for node in items.get("nodes") or []:
                cards.append(self._card_from_node(node))
            page_info = items.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
        return cards

    def all_cards(self):
        """Every card on the board, in every status — the unfiltered view
        that `pending()` and `stale()` each narrow. `teille-sync status`
        (counts per statut) and `teille-sync release` (looking a named
        card up regardless of where it sits) both need this; neither
        `pending()` nor `stale()` can stand in for it, since between them
        they cover only `À traiter` and an aged slice of `En cours`."""
        return self._all_cards()

    def pending(self):
        """Every card still `À traiter`, in title order.

        Title order is not a cosmetic choice: several machines run this
        tool against the same board, and starting from the same ordering
        is what makes two machines beginning at once collide on the same
        few cards — loudly, as a lost `claim()` — rather than silently
        pick disjoint-looking but overlapping sets from an API order that
        is not guaranteed stable between calls.
        """
        todo = [card for card in self._all_cards() if card.status == TODO]
        return sorted(todo, key=lambda card: card.identifier)

    def stale(self, older_than, now):
        """Cards stuck `En cours` whose claim stamp is older than
        `older_than` relative to `now`.

        The stamp is what `claim()` wrote: `"<machine> · <timestamp>"`.
        A card in `En cours` whose `Détail` is empty or does not parse as
        a stamp is left alone — it is not treated as stale. Guessing that
        an unreadable stamp means "abandoned" would hand another machine
        a document that a first machine is converting right now under a
        Détail it simply has not written yet, or has written in some
        other shape. Only a stamp this code can read, and can show is
        older than the threshold, justifies reclaiming the card.
        """
        overdue = []
        for card in self._all_cards():
            if card.status != WIP:
                continue
            age = self._claim_age(card.detail, now)
            if age is not None and age > older_than:
                overdue.append(card)
        return sorted(overdue, key=lambda card: card.identifier)

    @staticmethod
    def _claim_age(detail, now):
        """How long ago a `claim()` stamp was written, or `None` if
        `detail` is not a stamp `claim()` could have written."""
        if not detail or " · " not in detail:
            return None
        _, _, timestamp = detail.rpartition(" · ")
        try:
            claimed_at = datetime.fromisoformat(timestamp)
        except ValueError:
            return None
        return now - claimed_at
