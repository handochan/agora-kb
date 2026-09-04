"""The model-free deterministic ranking golden fixture (Stratum UNIT 3, gate B).

Pins what ``core.wiki.Wiki.query`` returns TODAY, over a synthetic corpus built by
:mod:`tests.support.kb_builder`, so the wiki layout axis flip (v1 ``wiki/<domain>/themes|daily``
→ the Stratum kind-first layout) can be ATTRIBUTED: ``core.wiki._is_moc_path`` reads the MOC out
of the PATH and seeds ``d_moc`` for the whole corpus, so the flip moves ranking whether or not
anyone intended it to.

Per ADR-0012 §0a nothing here computes a ``SearchHit`` field. The fixture only RECORDS what
``Wiki.query`` returns.
"""
