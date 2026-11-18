/*!
 * Image Retrieval arXiv Daily - paper explorer
 *
 * Renders data/papers.json (written by daily_arxiv.py) with instant search,
 * topic / period / code filters, sorting, shareable URLs and incremental
 * rendering, so the page stays fast however many papers are collected.
 * Abstracts are loaded on demand from data/abstracts/YYMM.json.
 * No dependencies; runs in all current browsers.
 */
(function () {
  'use strict';

  var PAGE_SIZE = 50;              // papers rendered per batch
  var AUTO_LOAD_LIMIT = 500;       // stop loading on scroll after this many papers
  var SEARCH_DELAY_MS = 140;       // debounce for typing in the search box
  var FETCH_TIMEOUT_MS = 30000;
  var AUTHORS_VISIBLE = 3;         // longer author lists are collapsed
  var BACK_TO_TOP_OFFSET = 900;    // px scrolled before the back-to-top button appears
  var DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  var RANGE_VALUES = ['latest', '7', '30', '90', '365'];
  var SORT_VALUES = ['newest', 'oldest'];
  var STATE_KEYS = ['q', 'topic', 'range', 'sort', 'code'];
  var TITLE_STOPWORDS = ['a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from', 'how', 'in', 'into',
    'is', 'it', 'of', 'on', 'or', 'the', 'to', 'toward', 'towards', 'via', 'what', 'when', 'with'];

  var root, els, statValues, statLabels, live;
  var data = null;                 // prepared dataset (see prepare())
  var paperById = new Map();
  var view = null;                 // current filtered list and rendering progress
  var state = { q: '', topic: '', range: '', sort: 'newest', code: false };
  var shardRequests = new Map();
  var searchTimer = 0;
  var topicNav = null;
  var topicOptions = new Map();
  var topicLinks = new Map();
  var observer = null;

  var numberFormat = new Intl.NumberFormat();
  var dayFormat = new Intl.DateTimeFormat(undefined, {
    weekday: 'short', year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC'
  });
  var shortDayFormat = new Intl.DateTimeFormat(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC'
  });
  var monthDayFormat = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
  // arXiv dates are UTC, so the update time is shown in UTC as well.
  var stampFormat = new Intl.DateTimeFormat(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    hourCycle: 'h23', timeZone: 'UTC', timeZoneName: 'short'
  });

  // ---------------------------------------------------------------------------
  // Small helpers
  // ---------------------------------------------------------------------------

  var ELLIPSIS = String.fromCharCode(0x2026);
  var HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (ch) { return HTML_ESCAPES[ch]; });
  }

  function text(value) {
    return typeof value === 'string' ? value : '';
  }

  function format(number) {
    return numberFormat.format(number);
  }

  function plural(count, word) {
    return format(count) + ' ' + word + (count === 1 ? '' : 's');
  }

  function utcDate(isoDay) {
    return new Date(isoDay + 'T00:00:00Z');
  }

  function formatDay(isoDay) {
    return dayFormat.format(utcDate(isoDay));
  }

  function formatShortDay(isoDay) {
    return shortDayFormat.format(utcDate(isoDay));
  }

  function safeUrl(value) {
    return typeof value === 'string' && /^https?:\/\/[^\s"<>]+$/i.test(value) ? value : '';
  }

  function domId(paperId) {
    return paperId.replace(/[^A-Za-z0-9_-]/g, '_');
  }

  function prefersReducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  // Case- and accent-insensitive folding that keeps the string length, so
  // match positions in the folded text map 1:1 onto the original text.
  var foldCache = new Map();
  var FOLD_RE = /[A-Z]|[^\x00-\x7F]/g;
  // Letters without a Unicode decomposition that readers type as plain ASCII:
  // dotless i, l with stroke, o with stroke, d with stroke, eth, h with stroke,
  // t with stroke and l with middle dot (given as code points to keep this file ASCII).
  var EXTRA_FOLDS = {};
  [[0x131, 'i'], [0x142, 'l'], [0xf8, 'o'], [0x111, 'd'], [0xf0, 'd'], [0x127, 'h'], [0x167, 't'],
    [0x140, 'l']].forEach(function (pair) {
    EXTRA_FOLDS[String.fromCharCode(pair[0])] = pair[1];
  });

  // True when a decomposed character is a base letter followed only by
  // combining diacritical marks (U+0300 to U+036F), i.e. an accented letter.
  function isAccentedLetter(decomposed) {
    if (decomposed.length < 2) {
      return false;
    }
    for (var i = 1; i < decomposed.length; i += 1) {
      var code = decomposed.charCodeAt(i);
      if (code < 0x300 || code > 0x36f) {
        return false;
      }
    }
    return true;
  }

  function foldChar(ch) {
    var out = foldCache.get(ch);
    if (out === undefined) {
      var lower = ch.toLowerCase();
      out = lower.length === 1 ? lower : ch;
      var decomposed = ch.normalize ? ch.normalize('NFD') : ch;
      if (isAccentedLetter(decomposed)) {
        var base = decomposed.charAt(0).toLowerCase();
        if (base.length === 1) {
          out = base;
        }
      }
      out = EXTRA_FOLDS[out] || out;
      foldCache.set(ch, out);
    }
    return out;
  }

  function fold(value) {
    return value.replace(FOLD_RE, foldChar);
  }

  // ---------------------------------------------------------------------------
  // Search terms and highlighting
  // ---------------------------------------------------------------------------

  // Reduces an arXiv URL or a versioned identifier to the plain arXiv ID.
  function normalizeTerm(term) {
    var url = /arxiv\.org\/(?:abs|pdf)\/([^\s?#]+?)(?:v\d+)?(?:\.pdf)?\/?(?:[?#].*)?$/.exec(term);
    if (url) {
      return url[1];
    }
    term = term.replace(/^arxiv:/, '');
    var versioned = /^(\d{4}\.\d{4,5})v\d+$/.exec(term);
    return versioned ? versioned[1] : term;
  }

  function parseTerms(query) {
    var terms = [];
    var pattern = /"([^"]+)"|(\S+)/g;
    var match;
    while ((match = pattern.exec(query))) {
      // An unpaired quotation mark (a phrase that is still being typed) is ignored.
      var raw = match[1] !== undefined ? match[1] : match[2].replace(/"/g, '');
      var term = normalizeTerm(fold(raw.trim()).replace(/\s+/g, ' '));
      if (term && terms.indexOf(term) === -1) {
        terms.push(term);
      }
    }
    return terms;
  }

  function highlight(value, folded, terms) {
    if (!terms.length || !value) {
      return escapeHtml(value);
    }
    var ranges = [];
    terms.forEach(function (term) {
      var index = folded.indexOf(term);
      while (index !== -1) {
        ranges.push([index, index + term.length]);
        index = folded.indexOf(term, index + term.length);
      }
    });
    if (!ranges.length) {
      return escapeHtml(value);
    }
    ranges.sort(function (a, b) { return a[0] - b[0] || b[1] - a[1]; });
    var out = '';
    var position = 0;
    var current = ranges[0].slice();
    for (var i = 1; i <= ranges.length; i += 1) {
      var next = ranges[i];
      if (next && next[0] <= current[1]) {
        current[1] = Math.max(current[1], next[1]);
        continue;
      }
      out += escapeHtml(value.slice(position, current[0])) + '<mark>' +
        escapeHtml(value.slice(current[0], current[1])) + '</mark>';
      position = current[1];
      if (next) {
        current = next.slice();
      }
    }
    return out + escapeHtml(value.slice(position));
  }

  // ---------------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------------

  function fetchJson(url) {
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = controller ? window.setTimeout(function () { controller.abort(); }, FETCH_TIMEOUT_MS) : 0;
    return fetch(url, { credentials: 'same-origin', signal: controller ? controller.signal : undefined })
      .then(function (response) {
        if (!response.ok) {
          throw new Error('the server answered HTTP ' + response.status);
        }
        return response.json();
      })
      .catch(function (error) {
        if (error && error.name === 'AbortError') {
          throw new Error('the request timed out');
        }
        throw error;
      })
      .finally(function () {
        window.clearTimeout(timer);
      });
  }

  function comparePapers(a, b) {
    if (a.published !== b.published) {
      return a.published < b.published ? 1 : -1;
    }
    return a.id < b.id ? 1 : (a.id > b.id ? -1 : 0);
  }

  function prepare(raw) {
    if (!raw || typeof raw !== 'object' || !Array.isArray(raw.papers)) {
      throw new Error('the data file has an unexpected format');
    }
    var topics = (Array.isArray(raw.topics) ? raw.topics : []).filter(function (topic) {
      return topic && typeof topic.id === 'string' && typeof topic.name === 'string';
    });
    var topicById = new Map(topics.map(function (topic) { return [topic.id, topic]; }));
    var papers = [];
    var seen = new Set();
    var latestAdded = '';
    var maxPublished = '';

    raw.papers.forEach(function (item) {
      if (!item || typeof item.id !== 'string' || !item.id || seen.has(item.id) ||
          typeof item.title !== 'string' || !DATE_RE.test(item.published)) {
        return;
      }
      var topicIds = (Array.isArray(item.topics) ? item.topics : []).filter(function (id) {
        return topicById.has(id);
      });
      if (!topicIds.length) {
        return;
      }
      seen.add(item.id);
      var authors = (Array.isArray(item.authors) ? item.authors : []).filter(function (name) {
        return typeof name === 'string' && name;
      });
      var categories = (Array.isArray(item.categories) ? item.categories : []).filter(function (name) {
        return typeof name === 'string';
      });
      var paper = {
        id: item.id,
        title: item.title,
        authors: authors,
        authorsText: authors.join(', '),
        published: item.published,
        updated: DATE_RE.test(item.updated) ? item.updated : item.published,
        primary: text(item.primary),
        categories: categories,
        comment: text(item.comment),
        journal: text(item.journal),
        doi: text(item.doi),
        code: safeUrl(item.code),
        topics: topicIds,
        added: DATE_RE.test(item.added) ? item.added : item.published
      };
      paper.titleFold = fold(paper.title);
      paper.authorsFold = fold(paper.authorsText);
      paper.idFold = fold(paper.id);
      paper.haystack = [
        paper.titleFold, paper.authorsFold, paper.idFold, fold(paper.comment), fold(paper.journal),
        fold(categories.join(' ')),
        fold(topicIds.map(function (id) { return topicById.get(id).name; }).join(' '))
      ].join('\n');
      papers.push(paper);
      if (paper.added > latestAdded) {
        latestAdded = paper.added;
      }
      if (paper.published > maxPublished) {
        maxPublished = paper.published;
      }
    });

    papers.sort(comparePapers);
    var latestCount = papers.filter(function (paper) { return paper.added === latestAdded; }).length;
    return {
      generated: text(raw.generated),
      topics: topics,
      topicById: topicById,
      papers: papers,
      latestAdded: latestAdded,
      latestCount: latestCount,
      maxPublished: maxPublished,
      codeCount: papers.filter(function (paper) { return paper.code; }).length,
      // Only flag "new" papers when older ones exist (not on the very first update).
      showNew: latestCount > 0 && latestCount < papers.length
    };
  }

  // ---------------------------------------------------------------------------
  // State and URL
  // ---------------------------------------------------------------------------

  function readUrlState() {
    var params = new URLSearchParams(window.location.search);
    var range = params.get('range') || '';
    var sort = params.get('sort') || '';
    state.q = (params.get('q') || '').slice(0, 200);
    state.topic = params.get('topic') || '';
    state.range = RANGE_VALUES.indexOf(range) !== -1 ? range : '';
    state.sort = SORT_VALUES.indexOf(sort) !== -1 ? sort : 'newest';
    state.code = params.get('code') === '1';
    if (data && state.topic && !data.topicById.has(state.topic)) {
      state.topic = '';
    }
  }

  function urlFor(next) {
    var params = new URLSearchParams(window.location.search);
    STATE_KEYS.forEach(function (key) { params.delete(key); });
    if (next.q) { params.set('q', next.q); }
    if (next.topic) { params.set('topic', next.topic); }
    if (next.range) { params.set('range', next.range); }
    if (next.sort !== 'newest') { params.set('sort', next.sort); }
    if (next.code) { params.set('code', '1'); }
    var query = params.toString();
    return window.location.pathname + (query ? '?' + query : '');
  }

  function syncUrl(push) {
    var url = urlFor(state);
    if (url === window.location.pathname + window.location.search) {
      return;
    }
    try {
      window.history[push ? 'pushState' : 'replaceState'](null, '', url + window.location.hash);
    } catch (error) {
      // History updates can fail (e.g. some file:// previews); filtering still works.
    }
  }

  function isFiltered() {
    return !!(state.q.trim() || state.topic || state.range || state.code);
  }

  function syncControls() {
    if (els.q.value !== state.q) {
      els.q.value = state.q;
    }
    // Until the topics are loaded the list only holds "All topics".
    els.topic.value = topicOptions.has(state.topic) ? state.topic : '';
    els.range.value = state.range;
    els.sort.value = state.sort;
    els.code.checked = state.code;
    els.reset.hidden = !(isFiltered() || state.sort !== 'newest');
  }

  // ---------------------------------------------------------------------------
  // Filtering
  // ---------------------------------------------------------------------------

  function rangeCutoff(range) {
    var days = parseInt(range, 10);
    if (!days || !data.maxPublished) {
      return '';
    }
    var date = utcDate(data.maxPublished);
    date.setUTCDate(date.getUTCDate() - (days - 1));
    return date.toISOString().slice(0, 10);
  }

  function applyFilters() {
    var terms = parseTerms(state.q);
    var cutoff = rangeCutoff(state.range);
    var counts = new Map();
    var matching = 0;
    var list = [];
    var dateCounts = new Map();

    data.papers.forEach(function (paper) {
      if (state.code && !paper.code) { return; }
      if (state.range === 'latest' ? paper.added !== data.latestAdded : (cutoff && paper.published < cutoff)) { return; }
      for (var i = 0; i < terms.length; i += 1) {
        if (paper.haystack.indexOf(terms[i]) === -1) { return; }
      }
      matching += 1;
      paper.topics.forEach(function (id) { counts.set(id, (counts.get(id) || 0) + 1); });
      if (!state.topic || paper.topics.indexOf(state.topic) !== -1) {
        list.push(paper);
        dateCounts.set(paper.published, (dateCounts.get(paper.published) || 0) + 1);
      }
    });
    if (state.sort === 'oldest') {
      list.reverse();
    }
    return { list: list, terms: terms, counts: counts, matching: matching, dateCounts: dateCounts };
  }

  // ---------------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------------

  function noticeHtml(title, message, action, actionLabel, isError) {
    return '<div class="pe-notice' + (isError ? ' pe-notice--error' : '') + '" role="' + (isError ? 'alert' : 'note') + '">' +
      '<strong>' + escapeHtml(title) + '</strong>' + escapeHtml(message) +
      (action ? '<br><button type="button" class="pe-btn" data-action="' + action + '">' + escapeHtml(actionLabel) + '</button>' : '') +
      '</div>';
  }

  function cardHtml(paper) {
    var terms = view.terms;
    var dom = domId(paper.id);
    var absUrl = 'https://arxiv.org/abs/' + paper.id;
    var pdfUrl = 'https://arxiv.org/pdf/' + paper.id;
    var html = '<li class="pe-card" data-id="' + escapeHtml(paper.id) + '"><article aria-labelledby="pe-t-' + dom + '">' +
      '<h3 class="pe-card__title" id="pe-t-' + dom + '"><a href="' + escapeHtml(absUrl) + '" target="_blank" rel="noopener">' +
      highlight(paper.title, paper.titleFold, terms) + '</a></h3>';

    if (paper.authors.length) {
      var collapse = paper.authors.length > AUTHORS_VISIBLE + 1;
      var shown = collapse ? paper.authors.slice(0, AUTHORS_VISIBLE).join(', ') : paper.authorsText;
      html += '<p class="pe-card__authors">' + highlight(shown, paper.authorsFold.slice(0, shown.length), terms) +
        (collapse ? '<button type="button" class="pe-card__more-authors" data-action="authors">+' +
          format(paper.authors.length - AUTHORS_VISIBLE) + ' more<span class="pe-visually-hidden"> authors</span></button>' : '') +
        '</p>';
    }

    var meta = [];
    if (data.showNew && paper.added === data.latestAdded) {
      meta.push('<span class="pe-badge pe-badge--new" title="Added in the latest update">New</span>');
    }
    meta.push('<span>arXiv:' + highlight(paper.id, paper.idFold, terms) + '</span>');
    if (paper.primary) {
      meta.push('<span title="Primary arXiv category">' + escapeHtml(paper.primary) + '</span>');
    }
    if (paper.updated !== paper.published) {
      meta.push('<span>Revised <time datetime="' + paper.updated + '">' + escapeHtml(formatShortDay(paper.updated)) + '</time></span>');
    }
    html += '<p class="pe-card__meta">' + meta.join('') + '</p>';

    if (paper.journal) {
      html += '<p class="pe-card__note"><b>Journal:</b> ' + highlight(paper.journal, fold(paper.journal), terms) + '</p>';
    }
    if (paper.comment) {
      html += '<p class="pe-card__note"><b>Comments:</b> ' + highlight(paper.comment, fold(paper.comment), terms) + '</p>';
    }

    var tags = paper.topics.map(function (id) {
      var active = state.topic === id;
      return '<button type="button" class="pe-tag" data-topic="' + escapeHtml(id) + '" aria-pressed="' + active + '" title="' +
        (active ? 'Show all topics' : 'Show only this topic') + '">' + escapeHtml(data.topicById.get(id).name) + '</button>';
    }).join('');

    var actions = '<a class="pe-action" href="' + escapeHtml(pdfUrl) + '" target="_blank" rel="noopener">PDF</a>';
    if (paper.code) {
      actions += '<a class="pe-action pe-action--code" href="' + escapeHtml(paper.code) + '" target="_blank" rel="noopener" title="' +
        escapeHtml(paper.code) + '">Code</a>';
    }
    actions += '<button type="button" class="pe-action" data-action="abstract" aria-expanded="false" aria-controls="pe-abs-' + dom + '">Abstract</button>' +
      '<button type="button" class="pe-action" data-action="bibtex" title="Copy a BibTeX entry">BibTeX</button>';

    return html + '<div class="pe-card__footer"><div class="pe-card__topics">' + tags + '</div>' +
      '<div class="pe-card__actions">' + actions + '</div></div>' +
      '<div class="pe-card__abstract" id="pe-abs-' + dom + '" hidden></div></article></li>';
  }

  function renderBatch() {
    var start = view.rendered;
    var end = Math.min(view.list.length, start + PAGE_SIZE);
    var index = start;
    while (index < end) {
      var day = view.list[index].published;
      var items = '';
      while (index < end && view.list[index].published === day) {
        items += cardHtml(view.list[index]);
        index += 1;
      }
      if (day === view.lastDay && view.lastList) {
        view.lastList.insertAdjacentHTML('beforeend', items);
      } else {
        var count = view.dateCounts.get(day) || 0;
        els.results.insertAdjacentHTML('beforeend',
          '<section class="pe-day" aria-label="' + escapeHtml(formatDay(day)) + '">' +
          '<h2 class="pe-day__title"><time datetime="' + day + '">' + escapeHtml(formatDay(day)) + '</time>' +
          '<span class="pe-day__count">' + plural(count, 'paper') + '</span></h2>' +
          '<ol class="pe-list">' + items + '</ol></section>');
        view.lastList = els.results.lastElementChild.querySelector('.pe-list');
        view.lastDay = day;
      }
    }
    view.rendered = end;
    renderMoreButton();
  }

  function renderMoreButton() {
    var remaining = view.list.length - view.rendered;
    els.more.hidden = remaining <= 0;
    if (remaining > 0) {
      els.moreBtn.textContent = 'Show ' + format(Math.min(PAGE_SIZE, remaining)) + ' more (' +
        format(remaining) + ' remaining)';
    }
  }

  function renderStatus() {
    var total = data.papers.length;
    if (!total) {
      els.status.textContent = '';
      return;
    }
    var order = state.sort === 'oldest' ? 'oldest first' : 'newest first';
    var shown = view.list.length;
    if (isFiltered()) {
      els.status.textContent = format(shown) + ' of ' + plural(total, 'paper') +
        (shown === 1 ? ' matches' : ' match') + ' your filters, ' + order + '.';
    } else {
      els.status.textContent = (total === 1 ? '1 paper' : 'All ' + plural(total, 'paper')) + ', ' + order + '.';
    }
  }

  function renderStats() {
    var total = data.papers.length;
    statValues.total.textContent = format(total);
    statValues.latest.textContent = format(data.latestCount);
    statValues.code.textContent = format(data.codeCount);
    statValues.topics.textContent = format(data.topics.length);
    if (data.latestAdded) {
      statLabels.latest.textContent = 'Added ' + monthDayFormat.format(utcDate(data.latestAdded));
    }
    statLabels.code.textContent = total ? 'With code (' + Math.round(100 * data.codeCount / total) + '%)' : 'With code';
    var stamp = data.generated ? new Date(data.generated) : null;
    if (stamp && !isNaN(stamp.getTime())) {
      els.updated.textContent = 'Updated ' + stampFormat.format(stamp);
      els.updated.title = data.generated;
    } else if (!total) {
      els.updated.textContent = 'Awaiting the first update';
    }
    var latestOption = els.range.querySelector('option[value="latest"]');
    if (latestOption && data.latestAdded) {
      latestOption.textContent = 'Latest update (' + formatShortDay(data.latestAdded) + ')';
    }
  }

  function buildTopicControls() {
    els.topic.textContent = '';
    topicOptions.clear();
    var all = document.createElement('option');
    all.value = '';
    els.topic.appendChild(all);
    topicOptions.set('', all);
    data.topics.forEach(function (topic) {
      var option = document.createElement('option');
      option.value = topic.id;
      option.title = text(topic.description);
      els.topic.appendChild(option);
      topicOptions.set(topic.id, option);
    });

    var siteNav = document.getElementById('site-nav');
    if (!siteNav || !data.topics.length) {
      return;
    }
    if (!topicNav) {
      topicNav = document.createElement('div');
      topicNav.className = 'pe-topic-nav';
      topicNav.addEventListener('click', onTopicLinkClick);
      siteNav.appendChild(topicNav);
    }
    topicNav.innerHTML = '<div class="nav-category" id="pe-topic-nav-title">Topics</div>' +
      '<ul class="nav-list" aria-labelledby="pe-topic-nav-title"></ul>';
    var list = topicNav.querySelector('ul');
    topicLinks.clear();
    [{ id: '', name: 'All topics', description: 'Show papers from every topic' }].concat(data.topics).forEach(function (topic) {
      var item = document.createElement('li');
      item.className = 'nav-list-item';
      item.innerHTML = '<a class="nav-list-link pe-topic-link" data-topic="' + escapeHtml(topic.id) + '" title="' +
        escapeHtml(text(topic.description)) + '"><span>' + escapeHtml(topic.name) + '</span>' +
        '<span class="pe-topic-link__count"></span></a>';
      list.appendChild(item);
      topicLinks.set(topic.id, item.firstChild);
    });
  }

  function renderFacets(result) {
    topicOptions.forEach(function (option, id) {
      var count = id ? (result.counts.get(id) || 0) : result.matching;
      var name = id ? data.topicById.get(id).name : 'All topics';
      option.textContent = name + ' (' + format(count) + ')';
    });
    els.topic.value = state.topic;
    topicLinks.forEach(function (link, id) {
      var count = id ? (result.counts.get(id) || 0) : result.matching;
      var active = state.topic === id;
      link.lastChild.textContent = format(count);
      link.href = urlFor({ q: state.q, topic: id, range: state.range, sort: state.sort, code: state.code });
      link.classList.toggle('active', active);
      if (active) {
        link.setAttribute('aria-current', 'true');
      } else {
        link.removeAttribute('aria-current');
      }
    });
  }

  function update(options) {
    options = options || {};
    if (!data) {  // still loading: keep the chosen filters and apply them when the data arrives
      syncControls();
      return;
    }
    var result = applyFilters();
    view = { list: result.list, terms: result.terms, dateCounts: result.dateCounts, rendered: 0, lastDay: '', lastList: null };
    els.results.textContent = '';
    els.results.setAttribute('aria-busy', 'false');
    if (!data.papers.length) {
      els.results.innerHTML = noticeHtml('No papers yet. ',
        'The collection is empty until the first daily update runs. Papers appear here automatically afterwards.');
    } else if (!view.list.length) {
      els.results.innerHTML = noticeHtml('No papers match your filters. ',
        'Try another search term, a wider period or a different topic.', 'reset', 'Reset filters');
    } else {
      renderBatch();
    }
    renderMoreButton();
    renderFacets(result);
    renderStatus();
    syncControls();
    if (options.push || options.replace) {
      syncUrl(!!options.push);
    }
    if (options.scroll) {
      var top = els.form.getBoundingClientRect().top;
      if (top < 0) {
        els.form.scrollIntoView({ block: 'start' });
      }
    }
    window.requestAnimationFrame(autoLoadIfVisible);
  }

  // ---------------------------------------------------------------------------
  // Card actions
  // ---------------------------------------------------------------------------

  function paperFor(element) {
    var card = element.closest('[data-id]');
    return card ? paperById.get(card.getAttribute('data-id')) : null;
  }

  function shardKey(paperId) {
    var match = /^(\d{4})\.\d{4,5}$/.exec(paperId);
    return match ? match[1] : 'legacy';
  }

  function loadShard(key) {
    if (!shardRequests.has(key)) {
      var version = root.getAttribute('data-version');
      var url = root.getAttribute('data-abstracts') + encodeURIComponent(key) + '.json' + (version ? '?v=' + encodeURIComponent(version) : '');
      var request = fetchJson(url).catch(function (error) {
        shardRequests.delete(key);  // allow a retry on the next click
        throw error;
      });
      shardRequests.set(key, request);
    }
    return shardRequests.get(key);
  }

  function toggleAbstract(button) {
    var panel = document.getElementById(button.getAttribute('aria-controls'));
    var paper = paperFor(button);
    if (!panel || !paper) {
      return;
    }
    if (button.getAttribute('aria-expanded') === 'true') {
      button.setAttribute('aria-expanded', 'false');
      panel.hidden = true;
      return;
    }
    button.setAttribute('aria-expanded', 'true');
    panel.hidden = false;
    if (panel.getAttribute('data-loaded') === 'abstract') {
      return;
    }
    panel.classList.add('is-loading');
    panel.textContent = 'Loading abstract' + ELLIPSIS;
    loadShard(shardKey(paper.id)).then(function (shard) {
      var abstract = shard && typeof shard[paper.id] === 'string' ? shard[paper.id] : '';
      panel.textContent = abstract || 'The abstract is not stored here. Open the paper on arXiv to read it.';
      if (abstract) {
        panel.setAttribute('data-loaded', 'abstract');
      }
    }, function () {
      panel.textContent = 'The abstract could not be loaded. Check your connection and try again.';
    }).then(function () {
      panel.classList.remove('is-loading');
    });
  }

  function texEscape(value) {
    var out = '';
    for (var i = 0; i < value.length; i += 1) {
      var ch = value.charAt(i);
      out += (ch === '&' || ch === '%' || ch === '#') && value.charAt(i - 1) !== '\\' ? '\\' + ch : ch;
    }
    return out;
  }

  function bibtexFor(paper) {
    var year = paper.published.slice(0, 4);
    var firstAuthor = paper.authors[0] || 'anonymous';
    var surname = fold(firstAuthor.split(/\s+/).pop()).replace(/[^a-z0-9]/g, '') || 'anonymous';
    var word = fold(paper.title).split(/[^a-z0-9]+/).filter(function (token) {
      return token.length > 2 && TITLE_STOPWORDS.indexOf(token) === -1;
    })[0] || 'paper';
    var lines = [
      '@misc{' + surname + year + word + ',',
      '  title         = {{' + texEscape(paper.title) + '}},',
      '  author        = {' + (paper.authors.map(texEscape).join(' and ') || 'Anonymous') + '},',
      '  year          = {' + year + '},',
      '  eprint        = {' + paper.id + '},',
      '  archivePrefix = {arXiv},'
    ];
    if (paper.primary) {
      lines.push('  primaryClass  = {' + paper.primary + '},');
    }
    if (paper.doi) {
      lines.push('  doi           = {' + texEscape(paper.doi) + '},');
    }
    lines.push('  url           = {https://arxiv.org/abs/' + paper.id + '}', '}');
    return lines.join('\n');
  }

  function copyText(value) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(value).then(function () { return true; }, function () { return legacyCopy(value); });
    }
    return Promise.resolve(legacyCopy(value));
  }

  function legacyCopy(value) {
    var area = document.createElement('textarea');
    area.value = value;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.top = '0';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try {
      ok = document.execCommand('copy');
    } catch (error) {
      ok = false;
    }
    document.body.removeChild(area);
    return ok;
  }

  function copyBibtex(button) {
    var paper = paperFor(button);
    if (!paper) {
      return;
    }
    var bibtex = bibtexFor(paper);
    copyText(bibtex).then(function (ok) {
      if (ok) {
        var label = button.textContent;
        button.textContent = 'Copied';
        button.classList.add('is-done');
        announce('BibTeX entry copied to the clipboard.');
        window.setTimeout(function () {
          button.textContent = label;
          button.classList.remove('is-done');
        }, 1800);
        return;
      }
      // Clipboard unavailable: show the entry so it can be copied by hand.
      var panel = document.getElementById('pe-abs-' + domId(paper.id));
      var abstractButton = button.parentNode.querySelector('[data-action="abstract"]');
      if (panel) {
        panel.hidden = false;
        panel.removeAttribute('data-loaded');
        panel.textContent = 'Copy this BibTeX entry:';
        var pre = document.createElement('pre');
        pre.textContent = bibtex;
        panel.appendChild(pre);
        if (abstractButton) {
          abstractButton.setAttribute('aria-expanded', 'true');
        }
      }
    });
  }

  function expandAuthors(button) {
    var paper = paperFor(button);
    var paragraph = button.parentNode;
    if (!paper || !paragraph) {
      return;
    }
    paragraph.innerHTML = highlight(paper.authorsText, paper.authorsFold, view.terms);
    paragraph.setAttribute('tabindex', '-1');
    paragraph.focus();
  }

  function announce(message) {
    live.textContent = '';
    window.setTimeout(function () { live.textContent = message; }, 30);
  }

  // ---------------------------------------------------------------------------
  // Events
  // ---------------------------------------------------------------------------

  // Take over text typed during the search delay, so that changing another
  // control in that moment does not discard it.
  function flushSearch() {
    window.clearTimeout(searchTimer);
    searchTimer = 0;
    state.q = els.q.value;
  }

  function setTopic(id, options) {
    flushSearch();
    state.topic = id && data && data.topicById.has(id) ? id : '';
    update({ push: true, scroll: options && options.scroll });
  }

  function setFilter(key, value) {
    flushSearch();
    state[key] = value;
    update({ replace: true });
  }

  function resetFilters() {
    window.clearTimeout(searchTimer);
    searchTimer = 0;
    state.q = '';
    state.topic = '';
    state.range = '';
    state.sort = 'newest';
    state.code = false;
    els.q.value = '';
    update({ push: true, scroll: true });
  }

  function onTopicLinkClick(event) {
    var link = event.target.closest('a[data-topic]');
    if (!link || event.defaultPrevented || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    setTopic(link.getAttribute('data-topic'), { scroll: true });
  }

  function onResultsClick(event) {
    var button = event.target.closest('button');
    if (!button || !els.results.contains(button)) {
      return;
    }
    if (button.classList.contains('pe-tag')) {
      var id = button.getAttribute('data-topic');
      setTopic(state.topic === id ? '' : id, { scroll: true });
      return;
    }
    switch (button.getAttribute('data-action')) {
      case 'abstract': toggleAbstract(button); break;
      case 'bibtex': copyBibtex(button); break;
      case 'authors': expandAuthors(button); break;
      case 'reset': resetFilters(); break;
      case 'retry': start(); break;
      default: break;
    }
  }

  function applySearch() {
    window.clearTimeout(searchTimer);
    searchTimer = 0;
    if (state.q !== els.q.value) {
      state.q = els.q.value;
      update({ replace: true });
    }
  }

  function isTypingTarget(target) {
    return !!target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
  }

  function autoLoadIfVisible() {
    if (!view || els.more.hidden || view.rendered >= AUTO_LOAD_LIMIT) {
      return;
    }
    var rect = els.more.getBoundingClientRect();
    if (rect.top < window.innerHeight + 800) {
      renderBatch();
      window.requestAnimationFrame(autoLoadIfVisible);
    }
  }

  function bindEvents() {
    els.form.addEventListener('submit', function (event) {
      event.preventDefault();
      applySearch();
      els.q.blur();
    });
    els.q.addEventListener('input', function () {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(applySearch, SEARCH_DELAY_MS);
    });
    els.q.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && els.q.value) {
        event.preventDefault();
        els.q.value = '';
        applySearch();
      }
    });
    els.topic.addEventListener('change', function () { setTopic(els.topic.value, {}); });
    els.range.addEventListener('change', function () { setFilter('range', els.range.value); });
    els.sort.addEventListener('change', function () { setFilter('sort', els.sort.value); });
    els.code.addEventListener('change', function () { setFilter('code', els.code.checked); });
    els.reset.addEventListener('click', resetFilters);
    els.moreBtn.addEventListener('click', function () { renderBatch(); });
    els.results.addEventListener('click', onResultsClick);

    document.addEventListener('keydown', function (event) {
      if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !isTypingTarget(event.target)) {
        event.preventDefault();
        els.q.focus();
        els.q.select();
      }
    });

    window.addEventListener('popstate', function () {
      window.clearTimeout(searchTimer);
      searchTimer = 0;
      readUrlState();
      update({});
    });

    if ('IntersectionObserver' in window) {
      observer = new IntersectionObserver(function (entries) {
        if (entries.some(function (entry) { return entry.isIntersecting; })) {
          autoLoadIfVisible();
        }
      }, { rootMargin: '0px 0px 800px 0px' });
      observer.observe(els.more);
    }

    var ticking = false;
    els.top.hidden = false;
    window.addEventListener('scroll', function () {
      if (ticking) {
        return;
      }
      ticking = true;
      window.requestAnimationFrame(function () {
        ticking = false;
        els.top.classList.toggle('is-visible', window.pageYOffset > BACK_TO_TOP_OFFSET);
      });
    }, { passive: true });
    els.top.addEventListener('click', function () {
      // Move focus first: focusing during a smooth scroll can cancel it.
      els.title.focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
    });
  }

  // ---------------------------------------------------------------------------
  // Start-up
  // ---------------------------------------------------------------------------

  function showLoadError(error) {
    els.results.setAttribute('aria-busy', 'false');
    els.status.textContent = '';
    els.more.hidden = true;
    els.results.innerHTML = noticeHtml('The paper list could not be loaded. ',
      'Reason: ' + (error && error.message ? error.message : 'unknown error') + '.', 'retry', 'Try again', true);
  }

  function start() {
    els.results.setAttribute('aria-busy', 'true');
    els.status.textContent = 'Loading papers' + ELLIPSIS;
    if (!data) {
      els.results.innerHTML = '<div class="pe-skeleton" aria-hidden="true"><div class="pe-skeleton__line"></div>' +
        '<div class="pe-skeleton__card"></div><div class="pe-skeleton__card"></div><div class="pe-skeleton__card"></div></div>';
    }
    fetchJson(root.getAttribute('data-papers')).then(function (raw) {
      data = prepare(raw);
      paperById = new Map(data.papers.map(function (paper) { return [paper.id, paper]; }));
      if (state.topic && !data.topicById.has(state.topic)) {
        state.topic = '';
      }
      if (els.q.value !== state.q) {  // the visitor may have typed while the data was loading
        state.q = els.q.value;
      }
      buildTopicControls();
      renderStats();
      update({ replace: true });
    }).catch(showLoadError);
  }

  function init() {
    root = document.getElementById('paper-explorer');
    if (!root) {
      return;
    }
    function find(selector) { return root.querySelector(selector); }
    els = {
      form: find('#pe-toolbar'), q: find('#pe-q'), topic: find('#pe-topic'), range: find('#pe-range'),
      sort: find('#pe-sort'), code: find('#pe-code'), reset: find('#pe-reset'), status: find('#pe-status'),
      results: find('#pe-results'), more: find('#pe-more'), moreBtn: find('#pe-more-btn'), top: find('#pe-top'),
      title: find('#pe-title'), updated: find('[data-pe-updated]')
    };
    statValues = {};
    statLabels = {};
    Array.prototype.forEach.call(root.querySelectorAll('[data-pe-stat]'), function (el) {
      statValues[el.getAttribute('data-pe-stat')] = el;
    });
    Array.prototype.forEach.call(root.querySelectorAll('[data-pe-stat-label]'), function (el) {
      statLabels[el.getAttribute('data-pe-stat-label')] = el;
    });
    live = document.createElement('div');
    live.className = 'pe-visually-hidden';
    live.setAttribute('aria-live', 'polite');
    root.appendChild(live);

    if (!window.fetch || !window.URLSearchParams || !window.Map || !Element.prototype.closest) {
      els.status.textContent = 'This browser is too old to display the interactive paper list.';
      return;
    }
    readUrlState();
    syncControls();
    bindEvents();
    start();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
