/* Closed expression interpreter: no eval or Function constructor. Firebase access is enabled only for a parsed, literal database URL. */
'use strict';
(() => {
  const model = JSON.parse(document.getElementById('swiftweb-model').textContent);
  const app = document.getElementById('app');
  const state = new Map();
  const query = new URLSearchParams(location.search);
  const firebaseDisabled = query.get('firebase') === 'off';
  const cacheDisabled = query.get('cache') === 'off';
  const persistenceKey = `swiftweb-state:${model.name}:${model.sourceId || 'v1'}`;
  if (!cacheDisabled) try {
    const saved = JSON.parse(localStorage.getItem(persistenceKey) || '[]');
    if (Array.isArray(saved)) for (const item of saved) {
      if (Array.isArray(item) && typeof item[0] === 'string') state.set(item[0], item[1]);
    }
  } catch { /* local file storage can be disabled by the browser */ }
  const pages = [{nodes: [model.root], ctx: '', locals: Object.create(null)}];
  const changedStateKeys = new Set();
  let pageTransition = '';
  const key = (ctx, name) => `${ctx}/${name}`;
  function persistState() {
    if (cacheDisabled) return;
    try { localStorage.setItem(persistenceKey, JSON.stringify([...state])); }
    catch { /* the app still works when browser storage is unavailable */ }
  }
  function stateKey(ctx, name) {
    let scope = ctx;
    while (!state.has(key(scope, name)) && scope.includes('/')) scope = scope.slice(0, scope.lastIndexOf('/'));
    return key(scope, name);
  }
  function bindingReference(expression) {
    if (!expression || typeof expression !== 'object') return null;
    if (expression.op === 'state') return {key: expression.key, path: []};
    if (expression.op === 'binding') return {key: expression.key, path: expression.path || []};
    if (expression.op === 'member' || expression.op === 'subscript') {
      const parent = bindingReference(expression.a);
      if (!parent) return null;
      const field = expression.op === 'member' ? expression.name
        : expression.key?.op === 'literal' ? expression.key.value : expression.key;
      return field === undefined ? null : {key: parent.key, path: [...parent.path, field]};
    }
    return null;
  }
  function readBinding(expression, ctx, locals) {
    const ref = bindingReference(expression);
    if (!ref) return null;
    const path = ref.path.map(field => typeof field === 'object' ? String(value(field, ctx, locals)) : String(field));
    return path.reduce((item, field) => item?.[field] ?? null, state.get(stateKey(ctx, ref.key)));
  }
  function writeBinding(expression, next, ctx, locals) {
    const ref = bindingReference(expression);
    if (!ref) return false;
    const resolved = stateKey(ctx, ref.key);
    const oldRoot = state.get(resolved);
    const path = ref.path.map(field => typeof field === 'object' ? String(value(field, ctx, locals)) : String(field));
    if (!path.length) state.set(resolved, next);
    else {
      const clone = value => Array.isArray(value) ? value.slice()
        : value && typeof value === 'object' ? {...value} : Object.create(null);
      const root = clone(oldRoot);
      let target = root, source = oldRoot;
      for (const field of path.slice(0, -1)) {
        const child = clone(source?.[field]);
        target[field] = child;
        target = child;
        source = source?.[field];
      }
      target[path[path.length - 1]] = next;
      state.set(resolved, root);
    }
    changedStateKeys.add(resolved);
    return true;
  }
  function expressionChanged(e, ctx) {
    if (!e || typeof e !== 'object') return false;
    if (e.op === 'state' && changedStateKeys.has(stateKey(ctx, e.key))) return true;
    return Object.values(e).some(value => Array.isArray(value)
      ? value.some(child => expressionChanged(child, ctx))
      : expressionChanged(value, ctx));
  }
  function animateVisibleContents(el) {
    if (!el || el.nodeType !== Node.ELEMENT_NODE) return;
    if (['component', 'group', 'windowgroup', 'navigationstack', 'navigationview'].some(name => el.classList.contains(name))) {
      for (const child of el.children) animateVisibleContents(child);
    } else {
      el.classList.add('swiftui-content-enter');
    }
  }
  function executeStatements(statements, ctx, locals, selfValue) {
    for (const statement of statements || []) {
      if (statement.kind === 'declare') {
        locals[statement.name] = value(statement.value, ctx, locals, selfValue);
      } else if (statement.kind === 'guardLet') {
        const bound = value(statement.value, ctx, locals, selfValue);
        if (bound == null) {
          const result = executeStatements(statement.else, ctx, locals, selfValue);
          if (result.returned || result.break) return result;
        } else {
          locals[statement.name] = bound;
        }
      } else if (statement.kind === 'guard') {
        const scope = Object.assign(Object.create(null), locals);
        let bound = true;
        for (const binding of statement.bindings || []) {
          const item = value(binding.value, ctx, scope, selfValue);
          if (item == null) { bound = false; break; }
          scope[binding.name] = item;
        }
        if (!bound || !value(statement.test, ctx, scope, selfValue)) {
          const result = executeStatements(statement.else, ctx, locals, selfValue);
          return result.returned || result.break ? result : {returned: true, value: result.value};
        }
        Object.assign(locals, scope);
      } else if (statement.kind === 'assign') {
        const old = locals[statement.name];
        const next = value(statement.value, ctx, locals, selfValue);
        locals[statement.name] = statement.operator === '+=' ? old + next
          : statement.operator === '-=' ? old - next : next;
      } else if (statement.kind === 'stateAssign') {
        const old = readBinding(statement.binding, ctx, locals);
        const next = value(statement.value, ctx, locals, selfValue);
        writeBinding(statement.binding, statement.operator === '+=' ? old + next
          : statement.operator === '-=' ? old - next : next, ctx, locals);
      } else if (statement.kind === 'subscriptAssign') {
        const target = locals[statement.name];
        if (target && typeof target === 'object') {
          const index = String(value(statement.key, ctx, locals, selfValue));
          const old = Object.hasOwn(target, index) ? target[index]
            : statement.default ? value(statement.default, ctx, locals, selfValue) : null;
          const next = value(statement.value, ctx, locals, selfValue);
          target[index] = statement.operator === '+=' ? old + next
            : statement.operator === '-=' ? old - next : next;
        }
      } else if (statement.kind === 'for') {
        const items = value(statement.items, ctx, locals, selfValue);
        if (Array.isArray(items)) {
          const existed = Object.hasOwn(locals, statement.name);
          const old = locals[statement.name];
          for (const [index, item] of items.entries()) {
            if (index >= 10000) break;
            locals[statement.name] = item;
            const result = executeStatements(statement.body, ctx, locals, selfValue);
            if (result.returned) return result;
            if (result.break) break;
          }
          if (existed) locals[statement.name] = old;
          else delete locals[statement.name];
        }
      } else if (statement.kind === 'if') {
        const branch = value(statement.test, ctx, locals, selfValue) ? statement.yes : statement.no;
        const result = executeStatements(branch, ctx, locals, selfValue);
        if (result.returned) return result;
        if (result.break) return result;
      } else if (statement.kind === 'switch') {
        const selected = value(statement.expression, ctx, locals, selfValue);
        const chosen = (statement.cases || []).find(item => !item.default &&
          item.values.some(pattern => swiftEqual(selected, value(pattern, ctx, locals, selfValue))))
          || (statement.cases || []).find(item => item.default);
        if (chosen) {
          const result = executeStatements(chosen.body, ctx, locals, selfValue);
          if (result.returned || result.break) return result;
        }
      } else if (statement.kind === 'break') {
        return {returned: false, break: true, value: undefined};
      } else if (statement.kind === 'expression') {
        value(statement.value, ctx, locals, selfValue);
      } else if (statement.kind === 'return') {
        return {returned: true, value: value(statement.value, ctx, locals, selfValue)};
      }
    }
    return {returned: false, value: undefined};
  }
  async function executeActionFunction(name, args, ctx, callerLocals, selfValue = null) {
    const fn = model.functions?.[name];
    if (!fn) throw new Error(`Translated helper ${name} is unavailable`);
    const locals = Object.create(null);
    for (const [parameter, expression] of Object.entries(args || {})) {
      locals[parameter] = await asyncValue(expression, ctx, callerLocals, selfValue);
    }
    return executeAsyncStatements(fn.body, ctx, locals, selfValue);
  }
  async function invokeClosureAsync(closure, args, ctx, locals, selfValue) {
    const scope = Object.assign(Object.create(null), closure.captures || {}, locals);
    (closure.params || []).forEach((name, index) => { scope[name] = args[index]; });
    const result = await executeAsyncStatements(closure.body, closure.context ?? ctx, scope, closure.selfValue ?? selfValue);
    return result.value;
  }
  async function callFirebaseBuiltin(name, args) {
    const api = window.swiftToWebFirebase;
    if (!api) throw new Error('Firebase is not available for this page');
    const path = args.path ?? '';
    if (name === 'readFirebase') return api.read(path);
    if (name === 'readFirebaseSnapshot') return api.readSnapshot(path);
    if (name === 'firebaseRequest') return api.request(path, args.method || 'GET', args.body, args.headers || {});
    if (name === 'writeFirebase') return api.write(path, args.data, {method: args.method || 'PUT', headers: args.headers || {}});
    if (name === 'updateFirebase') return api.write(path, args.data, {method: args.method || 'PUT'});
    if (name === 'createFirebase') {
      const response = await api.write(path, args.data, {method: 'POST'});
      return response?.name ?? null;
    }
    if (name === 'patchFirebaseRoot') return api.write('', args.updates, {method: 'PATCH'});
    throw new Error(`Unsupported Firebase operation ${name}`);
  }
  async function asyncValue(e, ctx, locals, selfValue = null) {
    if (!e || typeof e !== 'object') return e;
    if (e.op === 'dismissCall') {
      locals.__swiftwebDismiss?.();
      return null;
    }
    if (e.op === 'tryOptional') {
      try { return await asyncValue(e.a, ctx, locals, selfValue); }
      catch { return null; }
    }
    if (e.op === 'firebaseCall') {
      const args = Object.create(null);
      for (const [name, expression] of Object.entries(e.args || {})) {
        args[name] = await asyncValue(expression, ctx, locals, selfValue);
      }
      return callFirebaseBuiltin(e.name, args);
    }
    if (e.op === 'globalCall') {
      const fn = model.functions?.[e.name];
      if (fn?.builtin) return value(e, ctx, locals, selfValue);
      if (!fn) throw new Error(`Translated helper ${e.name} is unavailable`);
      return executeActionFunction(e.name, e.args, ctx, locals, selfValue);
    }
    if (e.op === 'componentCall') {
      const method = model.componentMethods?.[e.key];
      if (!method) throw new Error(`Translated method ${e.key} is unavailable`);
      const parameters = Object.create(null);
      for (const [name, expression] of Object.entries(e.args || {})) {
        parameters[name] = await asyncValue(expression, ctx, locals, selfValue);
      }
      return executeAsyncStatements(method.body, ctx, parameters, selfValue);
    }
    if (e.op === 'invoke') {
      const callee = value(e.callee, ctx, locals, selfValue);
      const args = [];
      for (const expression of e.args || []) args.push(await asyncValue(expression, ctx, locals, selfValue));
      if (callee?.op === 'closure') return invokeClosureAsync(callee, args, ctx, locals, selfValue);
      if (callee?.__type === 'JSONEncoder' && e.callee.name === 'encode') return args[0] ?? null;
      if (callee?.__type === 'FirebaseJSON' && ['object', 'array', 'string', 'integer', 'double', 'bool'].includes(e.callee.name)) return args[0] ?? null;
      if (callee?.__type === 'FirebaseJSON' && e.callee.name === 'serverTimestamp') return {'.sv': 'timestamp'};
      if (callee == null && e.callee.op === 'member') throw new Error(`Callback ${e.callee.name} is unavailable`);
      return null;
    }
    return value(e, ctx, locals, selfValue);
  }
  async function executeAsyncStatements(statements, ctx, locals, selfValue = null) {
    const deferred = [];
    try {
    for (const statement of statements || []) {
      if (statement.kind === 'defer') {
        deferred.push(statement.body || []);
      } else if (statement.kind === 'pathAssign') {
        const target = statement.target;
        const parentExpression = target?.op === 'member' || target?.op === 'subscript' ? target.a : null;
        const parent = value(parentExpression, ctx, locals, selfValue);
        const key = target?.op === 'member' ? target.name
          : target?.op === 'subscript' ? value(target.key, ctx, locals, selfValue) : null;
        if (!parent || key == null || typeof parent !== 'object') throw new Error('Unsupported translated assignment target');
        const old = parent[key], next = await asyncValue(statement.value, ctx, locals, selfValue);
        parent[key] = statement.operator === '+=' ? old + next
          : statement.operator === '-=' ? old - next : next;
      } else if (statement.kind === 'declare') {
        locals[statement.name] = await asyncValue(statement.value, ctx, locals, selfValue);
      } else if (statement.kind === 'assign') {
        const old = locals[statement.name], next = await asyncValue(statement.value, ctx, locals, selfValue);
        locals[statement.name] = statement.operator === '+=' ? old + next
          : statement.operator === '-=' ? old - next : next;
      } else if (statement.kind === 'stateAssign') {
        const old = readBinding(statement.binding, ctx, locals);
        const next = await asyncValue(statement.value, ctx, locals, selfValue);
        writeBinding(statement.binding, statement.operator === '+=' ? old + next
          : statement.operator === '-=' ? old - next : next, ctx, locals);
      } else if (statement.kind === 'guardLet') {
        const bound = await asyncValue(statement.value, ctx, locals, selfValue);
        if (bound == null) return executeAsyncStatements(statement.else, ctx, locals, selfValue);
        locals[statement.name] = bound;
      } else if (statement.kind === 'guard') {
        const scope = Object.assign(Object.create(null), locals);
        let bound = true;
        for (const binding of statement.bindings || []) {
          const item = await asyncValue(binding.value, ctx, scope, selfValue);
          if (item == null) { bound = false; break; }
          scope[binding.name] = item;
        }
        if (!bound || !await asyncValue(statement.test, ctx, scope, selfValue)) {
          const result = await executeAsyncStatements(statement.else, ctx, locals, selfValue);
          return result.returned || result.break ? result : {returned: true, value: result.value};
        }
        Object.assign(locals, scope);
      } else if (statement.kind === 'firebaseWrite') {
        const api = window.swiftToWebFirebase;
        if (!api?.write) throw new Error('Firebase write support is unavailable');
        await api.write(await asyncValue(statement.path, ctx, locals, selfValue),
          await asyncValue(statement.value, ctx, locals, selfValue));
      } else if (statement.kind === 'if') {
        const scope = Object.assign(Object.create(null), locals);
        let bound = true;
        for (const binding of statement.bindings || []) {
          const item = await asyncValue(binding.value, ctx, scope, selfValue);
          if (item == null) { bound = false; break; }
          scope[binding.name] = item;
        }
        const result = await executeAsyncStatements(bound && await asyncValue(statement.test, ctx, scope, selfValue)
          ? statement.yes : statement.no, ctx, scope, selfValue);
        if (result.returned || result.break) return result;
      } else if (statement.kind === 'switch') {
        const selected = await asyncValue(statement.expression, ctx, locals, selfValue);
        let branch = statement.cases?.find(item => (item.values || []).some(candidate =>
          swiftEqual(value(candidate, ctx, locals, selfValue), selected)));
        branch ||= statement.cases?.find(item => item.default);
        if (branch) {
          const result = await executeAsyncStatements(branch.body, ctx, locals, selfValue);
          if (result.returned || result.break) return result;
        }
      } else if (statement.kind === 'for') {
        const items = await asyncValue(statement.items, ctx, locals, selfValue);
        if (Array.isArray(items)) for (const [index, item] of items.entries()) {
          if (index >= 10000) break;
          const existed = Object.hasOwn(locals, statement.name), old = locals[statement.name];
          const names = statement.names || [statement.name];
          if (names.length > 1) {
            const tuple = Array.isArray(item) ? item
              : item && typeof item === 'object' ? [item.offset ?? item.key, item.element ?? item.value] : [item];
            names.forEach((name, position) => { locals[name] = tuple[position]; });
          } else locals[statement.name] = item;
          const result = await executeAsyncStatements(statement.body, ctx, locals, selfValue);
          if (names.length > 1) names.forEach(name => { if (name === statement.name && existed) locals[name] = old; else delete locals[name]; });
          else if (existed) locals[statement.name] = old; else delete locals[statement.name];
          if (result.returned) return result;
          if (result.continue) continue;
          if (result.break) break;
        }
      } else if (statement.kind === 'doCatch') {
        try {
          const result = await executeAsyncStatements(statement.body, ctx, locals, selfValue);
          if (result.returned || result.break) return result;
        } catch (error) {
          locals[statement.catchName || 'error'] = error;
          let result;
          try {
            if (statement.catchTest && !await asyncValue(statement.catchTest, ctx, locals, selfValue)) throw error;
            result = await executeAsyncStatements(statement.catch, ctx, locals, selfValue);
          } finally {
            delete locals[statement.catchName || 'error'];
          }
          if (result.returned || result.break) return result;
        }
      } else if (statement.kind === 'task') {
        executeAsyncStatements(statement.body, ctx, locals, selfValue).catch(() =>
          showRuntimeIssue('The asynchronous action failed. Check Firebase access and the conversion report.'));
      } else if (statement.kind === 'throw') {
        const error = await asyncValue(statement.value, ctx, locals, selfValue);
        const thrown = new Error(error?.message || error?.rawValue || error?.__enumCase || String(error ?? 'Swift operation failed'));
        thrown.statusCode = error?.statusCode;
        thrown.swiftType = error?.swiftType || error?.__type || null;
        thrown.__enumCase = error?.__enumCase || null;
        thrown.rawValue = error?.rawValue;
        throw thrown;
      } else if (statement.kind === 'break') {
        return {returned: false, break: true, value: undefined};
      } else if (statement.kind === 'continue') {
        return {returned: false, continue: true, value: undefined};
      } else if (statement.kind === 'return') {
        return {returned: true, value: await asyncValue(statement.value, ctx, locals, selfValue)};
      } else if (statement.kind === 'expression') {
        await asyncValue(statement.value, ctx, locals, selfValue);
      } else {
        throw new Error(`Unsupported translated helper statement: ${statement.kind}`);
      }
    }
    return {returned: false, value: undefined};
    } finally {
      for (const body of deferred.reverse()) await executeAsyncStatements(body, ctx, locals, selfValue);
    }
  }
  function invokeClosure(closure, args, ctx, locals, selfValue) {
    const scope = Object.assign(Object.create(null), closure.captures || {}, locals);
    (closure.params || []).forEach((name, index) => { scope[name] = args[index]; });
    return executeStatements(closure.body, closure.context ?? ctx, scope, closure.selfValue ?? selfValue).value;
  }
  function value(e, ctx, locals, selfValue = null) {
    if (e == null) return null;
    if (typeof e !== 'object') return e;
    switch (e.op) {
      case 'literal': return e.value;
      case 'tryOptional': return value(e.a, ctx, locals, selfValue);
      case 'state': return state.get(stateKey(ctx, e.key));
      case 'binding': return readBinding(e, ctx, locals);
      case 'local': return locals[e.key];
      case 'computed': return executeStatements(model.computedProperties?.[e.key], ctx, locals, selfValue).value;
      case 'self': return selfValue;
      case 'type': return {__type: e.name};
      case 'enumCase': {
        if (e.name === 'orderedAscending') return -1;
        if (e.name === 'orderedSame') return 0;
        if (e.name === 'orderedDescending') return 1;
        return {__type: e.type, __enumCase: e.name, rawValue: model.enumCaseRawValues?.[e.name] ?? e.name,
          associated: (e.args || []).map(item => value(item, ctx, locals, selfValue))};
      }
      case 'calendar': return {__type: 'Calendar'};
      case 'closure': return {...e, captures: {...locals}, context: ctx, selfValue};
      case 'tuple': return e.items.map(x => value(x, ctx, locals, selfValue));
      case 'date': return new Date((Number(value(e.seconds, ctx, locals, selfValue)) + 978307200) * 1000);
      case 'date1970': return new Date(Number(value(e.seconds, ctx, locals, selfValue)) * 1000);
      case 'nowDate': return new Date();
      case 'convert': {
        const raw = value(e.value, ctx, locals, selfValue);
        if (raw == null) return null;
        if (e.type === 'String') return String(raw);
        if (e.type === 'Bool') return Boolean(raw);
        const numeric = Number(raw);
        if (!Number.isFinite(numeric)) return null;
        return e.type === 'Double' ? numeric : Math.trunc(numeric);
      }
      case 'format': {
        const args = (e.args || []).map(item => value(item, ctx, locals, selfValue));
        let index = 0;
        return e.template.replace(/%%|%0?(\d*)d/g, (token, width) => {
          if (token === '%%') return '%';
          const digits = Number(args[index++] || 0);
          return String(Math.trunc(digits)).padStart(Number(width || 0), '0');
        });
      }
      case 'array': return e.items.map(x => value(x, ctx, locals, selfValue));
      case 'reduce': {
        const items = value(e.a, ctx, locals, selfValue) || [];
        let accumulator = value(e.initial, ctx, locals, selfValue);
        const closure = e.closure || {params: ['$0', '$1'], body: []};
        for (const item of items) {
          const scope = Object.assign(Object.create(null), locals);
          scope[closure.params?.[0] || '$0'] = accumulator;
          scope[closure.params?.[1] || '$1'] = item;
          const result = executeStatements(closure.body, ctx, scope, selfValue);
          accumulator = result.returned ? result.value : scope[closure.params?.[0] || '$0'];
        }
        return accumulator;
      }
      case 'construct': {
        if (e.type === 'UUID') return {__type: 'UUID', uuidString: crypto.randomUUID().toLowerCase()};
        const object = {__type: e.type};
        for (const [field, expression] of Object.entries(model.modelDefaults?.[e.type] || {})) {
          object[field] = value(expression, ctx, locals, selfValue);
        }
        for (const field of e.fields || []) object[field.name] = value(field.value, ctx, locals, selfValue);
        return object;
      }
      case 'dictionary': return Object.fromEntries((e.entries || []).map(([key, item]) => [value(key, ctx, locals, selfValue), value(item, ctx, locals, selfValue)]));
      case 'member': {
        const target = value(e.a, ctx, locals, selfValue);
        if (target instanceof Date && e.name === 'timeIntervalSince1970') return target.getTime() / 1000;
        if (target?.__type === 'UUID' && e.name === 'uuidString') return target.uuidString;
        if (target instanceof Error && ['localizedDescription', 'errorDescription', 'message'].includes(e.name)) return target.message;
        if (target?.__enumCase && ['localizedDescription', 'errorDescription'].includes(e.name)) {
          return target.localizedDescription ?? target.errorDescription ?? target.rawValue ?? target.__enumCase;
        }
        if (target != null && ['stringValue', 'intValue', 'doubleValue', 'boolValue', 'arrayValue', 'objectValue'].includes(e.name)) {
          if (e.name === 'stringValue') return typeof target === 'string' ? target : null;
          if (e.name === 'intValue') return Number.isFinite(Number(target)) ? Math.trunc(Number(target)) : null;
          if (e.name === 'doubleValue') return Number.isFinite(Number(target)) ? Number(target) : null;
          if (e.name === 'boolValue') return typeof target === 'boolean' ? target : null;
          if (e.name === 'arrayValue') return Array.isArray(target) ? target : null;
          if (e.name === 'objectValue') return target && typeof target === 'object' && !Array.isArray(target) ? target : null;
        }
        if (target?.__type && e.name === 'allCases') {
          return (model.enums?.[target.__type] || []).map(item => ({__type: target.__type,
            __enumCase: item.name, rawValue: item.rawValue, associated: []}));
        }
        if (target?.__enumCase && e.name === 'rawValue') return target.rawValue;
        if (target && typeof target === 'object' && e.name === 'keys') return Object.keys(target);
        if (target && typeof target === 'object' && e.name === 'values') return Object.values(target);
        return target?.[e.name] ?? null;
      }
      case 'call': {
        const receiver = value(e.a, ctx, locals, selfValue);
        const args = (e.args || []).map(x => value(x, ctx, locals, selfValue));
        if (receiver?.__type === 'Calendar' && e.name === 'component') {
          const date = args[1];
          if (!(date instanceof Date)) return null;
          if (args[0] === 'day') return date.getDate();
          if (args[0] === 'month') return date.getMonth() + 1;
          if (args[0] === 'year') return date.getFullYear();
        }
        if (receiver?.__type === 'min') return Math.min(...args.map(Number));
        if (receiver?.__type === 'max') return Math.max(...args.map(Number));
        if (receiver?.__enumCase && e.name === 'opacity') return {__color: receiver.__enumCase, opacity: Number(args[0] ?? 1)};
        if (receiver instanceof Date && e.name === 'formatted') {
          try {
            const withTime = args.some(item => item?.__enumCase === 'shortened');
            return new Intl.DateTimeFormat('es-ES', withTime
              ? {dateStyle: 'medium', timeStyle: 'short'} : {dateStyle: 'medium'}).format(receiver);
          } catch { return receiver.toLocaleDateString('es-ES'); }
        }
        if (Array.isArray(receiver)) {
          if (e.name === 'removeFirst' && !args.length) return receiver.shift() ?? null;
          if (e.name === 'append' && args.length === 1) {
            receiver.push(args[0]);
            if (e.a?.op === 'state') changedStateKeys.add(stateKey(ctx, e.a.key));
            return null;
          }
          if (e.name === 'removeAll' && !args.length) {
            receiver.length = 0;
            if (e.a?.op === 'state') changedStateKeys.add(stateKey(ctx, e.a.key));
            return null;
          }
          if (e.name === 'removeAll' && args[0]?.op === 'closure') {
            const kept = receiver.filter(item => !invokeClosure(args[0], [item], ctx, locals, selfValue));
            receiver.splice(0, receiver.length, ...kept);
            if (e.a?.op === 'state') changedStateKeys.add(stateKey(ctx, e.a.key));
            return null;
          }
          if (e.name === 'contains' && args.length === 1) return receiver.includes(args[0]);
          if (e.name === 'sorted' && !args.length) return receiver.slice().sort((a, b) =>
            typeof a === 'string' && typeof b === 'string' ? a.localeCompare(b, 'es') : Number(a) - Number(b));
          if (e.name === 'enumerated' && !args.length) return receiver.map((item, offset) => ({offset, element: item}));
          if (e.name === 'uniqued' && !args.length) return [...new Set(receiver)];
          if (e.name === 'map' && args[0]?.op === 'closure') return receiver.map(item => invokeClosure(args[0], [item], ctx, locals, selfValue));
          if (e.name === 'mapValues' && args[0]?.op === 'closure') return Object.fromEntries(
            Object.entries(receiver).map(([key, item]) => [key, invokeClosure(args[0], [item], ctx, locals, selfValue)]));
          if (e.name === 'compactMap' && args[0]?.op === 'closure') return receiver.map(item => invokeClosure(args[0], [item], ctx, locals, selfValue)).filter(item => item != null);
          if (e.name === 'flatMap' && args[0]?.op === 'closure') return receiver.flatMap(item => {
            const result = invokeClosure(args[0], [item], ctx, locals, selfValue);
            return Array.isArray(result) ? result : result == null ? [] : [result];
          });
          if (e.name === 'filter' && args[0]?.op === 'closure') return receiver.filter(item => Boolean(invokeClosure(args[0], [item], ctx, locals, selfValue)));
          if (e.name === 'first' && args[0]?.op === 'closure') return receiver.find(item => Boolean(invokeClosure(args[0], [item], ctx, locals, selfValue))) ?? null;
          if (e.name === 'joined') return receiver.map(item => String(item ?? '')).join(String(args[0] ?? ''));
        }
        if (receiver && !Array.isArray(receiver) && typeof receiver === 'object') {
          const applyEntry = (closure, key, item) => closure.params?.length > 1
            ? invokeClosure(closure, [key, item], ctx, locals, selfValue)
            : invokeClosure(closure, [{key, value: item}], ctx, locals, selfValue);
          if (e.name === 'map' && args[0]?.op === 'closure') return Object.entries(receiver).map(([key, item]) => applyEntry(args[0], key, item));
          if (e.name === 'compactMap' && args[0]?.op === 'closure') return Object.entries(receiver).map(([key, item]) => applyEntry(args[0], key, item)).filter(item => item != null);
          if (e.name === 'flatMap' && args[0]?.op === 'closure') return Object.entries(receiver).flatMap(([key, item]) => {
            const result = applyEntry(args[0], key, item);
            return Array.isArray(result) ? result : result == null ? [] : [result];
          });
          if (e.name === 'filter' && args[0]?.op === 'closure') return Object.entries(receiver).filter(([key, item]) => applyEntry(args[0], key, item)).map(([key, item]) => ({key, value: item}));
          if (e.name === 'contains' && args.length === 1) return Object.hasOwn(receiver, String(args[0]));
        }
        if (typeof receiver === 'string' && e.name === 'localizedCaseInsensitiveCompare' && args.length === 1) {
          const left = receiver.toLocaleLowerCase(), right = String(args[0]).toLocaleLowerCase();
          return left < right ? -1 : left > right ? 1 : 0;
        }
        if (typeof receiver === 'string' && e.name === 'trimmingCharacters') return receiver.trim();
        if (receiver?.__type === 'JSONEncoder' && e.name === 'encode') return args[0] ?? null;
        if (receiver?.__type === 'FirebaseJSON' && ['object', 'array', 'string', 'integer', 'double', 'bool'].includes(e.name)) return args[0] ?? null;
        if (receiver?.__type === 'FirebaseJSON' && e.name === 'serverTimestamp') return {'.sv': 'timestamp'};
        const method = receiver?.__type && model.methods?.[`${receiver.__type}.${e.name}`];
        if (!method || (e.args || []).length !== (method.params || []).length) return null;
        const methodLocals = Object.create(null);
        (method.params || []).forEach((parameter, index) => { methodLocals[parameter.name] = args[index]; });
        return executeStatements(method.body, ctx, methodLocals, receiver).value;
      }
      case 'dictionaryCounts': {
        const pairs = value(e.a, ctx, locals, selfValue);
        const counts = Object.create(null);
        for (const pair of pairs || []) {
          if (Array.isArray(pair) && pair.length >= 2) counts[String(pair[0])] = (counts[String(pair[0])] || 0) + Number(pair[1] || 0);
        }
        return {__dictionary: true, values: counts};
      }
      case 'subscript': {
        const target = value(e.a, ctx, locals, selfValue);
        const index = value(e.key, ctx, locals, selfValue);
        const found = target?.__dictionary ? target.values[String(index)] : target?.[index];
        return found ?? (e.default ? value(e.default, ctx, locals, selfValue) : null);
      }
      case 'invoke': return null;
      case 'firebaseCall': return null;
      case 'globalCall': {
        const fn = model.functions?.[e.name];
        if (!fn) return null;
        const parameters = Object.create(null);
        for (const [name, expression] of Object.entries(e.args || {})) {
          parameters[name] = value(expression, ctx, locals, selfValue);
        }
        if (fn.builtin === 'spanishSchoolDateLabel') {
          const key = String(parameters.key ?? '');
          if (!/^\d{4}-\d{2}-\d{2}$/.test(key)) return key;
          const date = new Date(`${key}T12:00:00Z`);
          if (Number.isNaN(date.valueOf())) return key;
          const label = new Intl.DateTimeFormat('es-ES', {
            weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC',
          }).format(date);
          return label.charAt(0).toLocaleUpperCase('es-ES') + label.slice(1);
        }
        if (fn.builtin === 'spanishSchoolDayKey') {
          const date = parameters.matchDate instanceof Date ? parameters.matchDate : new Date(parameters.matchDate);
          if (Number.isNaN(date.valueOf())) return '';
          const parts = new Intl.DateTimeFormat('en-GB', {
            year: 'numeric', month: '2-digit', day: '2-digit', timeZone: 'Europe/Madrid',
          }).formatToParts(date);
          const fields = Object.fromEntries(parts.map(item => [item.type, item.value]));
          return `${fields.year}-${fields.month}-${fields.day}`;
        }
        return executeStatements(fn.body, ctx, parameters, null).value;
      }
      case 'componentCall': {
        const fn = model.componentMethods?.[e.key];
        if (!fn) return null;
        const parameters = Object.create(null);
        for (const [name, expression] of Object.entries(e.args || {})) {
          parameters[name] = value(expression, ctx, locals, selfValue);
        }
        return executeStatements(fn.body, ctx, parameters, selfValue).value;
      }
      case 'sorted': {
        const items = value(e.a, ctx, locals, selfValue);
        if (!Array.isArray(items)) return [];
        const [firstName, secondName] = e.params || ['$0', '$1'];
        return items.slice().sort((first, second) => {
          const firstScope = {...locals, [firstName]: first, [secondName]: second};
          const forward = e.closure ? invokeClosure(e.closure, [first, second], ctx, locals, selfValue)
            : value(e.compare, ctx, firstScope, selfValue);
          if (forward) return -1;
          const secondScope = {...locals, [firstName]: second, [secondName]: first};
          const reverse = e.closure ? invokeClosure(e.closure, [second, first], ctx, locals, selfValue)
            : value(e.compare, ctx, secondScope, selfValue);
          return reverse ? 1 : 0;
        });
      }
      case 'extremumBy': {
        const items = value(e.a, ctx, locals, selfValue);
        if (!Array.isArray(items) || !items.length) return null;
        const [firstName, secondName] = e.params || ['$0', '$1'];
        return items.reduce((best, candidate) => {
          const scope = {...locals, [firstName]: candidate, [secondName]: best};
          const candidateFirst = Boolean(value(e.compare, ctx, scope, selfValue));
          return e.kind === 'min' ? (candidateFirst ? candidate : best) : (candidateFirst ? best : candidate);
        });
      }
      case 'prefix': {
        const a = value(e.a, ctx, locals, selfValue);
        const count = Math.max(0, Math.trunc(Number(value(e.count, ctx, locals, selfValue))) || 0);
        return Array.from(a ?? '').slice(0, count);
      }
      case 'concat': return e.items.map(x => String(value(x, ctx, locals, selfValue) ?? '')).join('');
      case 'conditional': return value(e.test, ctx, locals, selfValue) ? value(e.yes, ctx, locals, selfValue) : value(e.no, ctx, locals, selfValue);
      case 'property': {
        const a = value(e.a, ctx, locals, selfValue);
        if (e.kind === 'count') return a?.length ?? 0;
        if (e.kind === 'isEmpty') return (a?.length ?? 0) === 0;
        if (e.kind === 'first') return Array.isArray(a) ? (a[0] ?? null) : null;
        if (e.kind === 'last') return Array.isArray(a) ? (a[a.length - 1] ?? null) : null;
        return null;
      }
      case 'unwrap': return value(e.a, ctx, locals, selfValue) ?? null;
      case 'cast': {
        const target = value(e.a, ctx, locals, selfValue);
        if (target == null) return null;
        if (target.swiftType === e.type || target.__type === e.type) return target;
        if (e.type === 'FirebaseHTTPError' && typeof target.statusCode === 'number') return target;
        return null;
      }
      case 'unary': return e.kind === '!' ? !value(e.a, ctx, locals, selfValue) : -value(e.a, ctx, locals, selfValue);
      case 'binary': {
        const a = value(e.a, ctx, locals, selfValue);
        if (e.kind === '??') return a ?? value(e.b, ctx, locals, selfValue);
        if (e.kind === '&&') return a && value(e.b, ctx, locals, selfValue);
        if (e.kind === '||') return a || value(e.b, ctx, locals, selfValue);
        const b = value(e.b, ctx, locals, selfValue);
        switch (e.kind) {
          case '+': return a + b;
          case '-': return a - b;
          case '*': return a * b;
          case '/': return a / b;
          case '%': return a % b;
          case '==': return swiftEqual(a, b);
          case '!=': return !swiftEqual(a, b);
          case '<': return a < b;
          case '>': return a > b;
          case '<=': return a <= b;
          case '>=': return a >= b;
          case '...': return Array.from({length: Math.max(0, Math.min(10000, Math.trunc(Number(b) - Number(a) + 1)))}, (_, i) => Number(a) + i);
          case '..<': return Array.from({length: Math.max(0, Math.min(10000, Math.trunc(Number(b) - Number(a))))}, (_, i) => Number(a) + i);
        }
      }
    }
    return null;
  }
  function swiftEqual(a, b) {
    if (a?.__enumCase && b?.__enumCase) return a.__enumCase === b.__enumCase;
    return a === b;
  }
  async function actions(list, ctx, locals) {
    for (const a of list) {
      if (a.kind === 'program') {
        await executeAsyncStatements(a.statements, ctx, locals);
        continue;
      }
      if (a.kind === 'functionCall') {
        try {
          await executeActionFunction(a.name, a.args, ctx, locals);
        } catch {
          showRuntimeIssue('The translated action failed. Check the conversion report, network, and Firebase access rules.');
        }
        continue;
      }
      if (a.kind === 'firebaseWrite') {
        try {
          await window.swiftToWebFirebase.write(value(a.path, ctx, locals), value(a.value, ctx, locals));
        } catch {
          showRuntimeIssue('Firebase write failed. Check the database URL, network, and access rules.');
        }
        continue;
      }
      if (a.kind === 'closureCall') {
        const callback = a.closure ? value(a.closure, ctx, locals) : locals[a.name];
        if (callback?.op === 'closure') {
          const args = (a.args || []).map(expression => value(expression, ctx, locals));
          await invokeClosureAsync(callback, args, ctx, locals, null);
        } else showRuntimeIssue(`Callback ${a.name} could not be translated.`);
        continue;
      }
      if (a.kind === 'componentMethodCall') {
        const method = model.componentMethods?.[a.key];
        if (method) {
          const localsForMethod = Object.create(null);
          (method.params || []).forEach((parameter, index) => {
            localsForMethod[parameter.name] = value(a.args?.[index], ctx, locals);
          });
          await executeAsyncStatements(method.body, ctx, localsForMethod, null);
        }
        continue;
      }
      if (a.kind === 'collection') {
        const current = readBinding(a.binding, ctx, locals);
        const next = Array.isArray(current) ? current.slice() : [];
        if (a.method === 'append') next.push(value(a.args?.[0], ctx, locals));
        else if (a.method === 'removeAll') next.length = 0;
        writeBinding(a.binding, next, ctx, locals);
        continue;
      }
      const k = stateKey(ctx, a.key), old = state.get(k);
      const v = value(a.value, ctx, locals);
      state.set(k, a.kind === 'toggle' ? !old : a.kind === '+=' ? old + v : a.kind === '-=' ? old - v : v);
      changedStateKeys.add(k);
    }
    persistState();
    render();
  }
  const element = (tag, cls) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    return el;
  };
  function findNavigationDestination(nodes) {
    for (const node of nodes || []) {
      if (!node || typeof node !== 'object') continue;
      if (node.destination && node.destinationParam) {
        return {content: node.destination, param: node.destinationParam};
      }
      for (const [name, child] of Object.entries(node)) {
        if (name === 'destination' || name === 'assets' || name === 'src') continue;
        const found = Array.isArray(child) ? findNavigationDestination(child)
          : child && typeof child === 'object' ? findNavigationDestination([child]) : null;
        if (found) return found;
      }
    }
    return null;
  }
  function symbolGlyph(raw) {
    const name = raw?.__enumCase || raw?.rawValue || raw;
    const glyphs = {
      plus: '+', trash: '⌫', circle: '○', 'circle.fill': '●', 'checkmark.circle.fill': '✓',
      'checkmark.circle': '✓', checkmark: '✓', 'chevron.right': '›', 'chevron.left': '‹',
      'person.fill': '●', 'pencil': '✎', 'xmark': '×', 'arrow.up': '↑', 'arrow.down': '↓',
      'star.fill': '★', 'exclamationmark.triangle.fill': '⚠', 'clock.fill': '◷',
    };
    return glyphs[String(name)] || '•';
  }
  function cssColor(raw) {
    if (raw && typeof raw === 'object' && raw.__color) {
      const base = cssColor(raw.__color), alpha = Math.max(0, Math.min(1, Number(raw.opacity)));
      if (base.startsWith('#') && (base.length === 4 || base.length === 7)) {
        const hex = base.length === 4 ? base.slice(1).split('').map(x => x + x).join('') : base.slice(1);
        const red = parseInt(hex.slice(0, 2), 16), green = parseInt(hex.slice(2, 4), 16), blue = parseInt(hex.slice(4, 6), 16);
        return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
      }
      return base;
    }
    const name = raw?.__enumCase || raw?.rawValue || raw;
    const colors = {
      red: '#ff3b30', orange: '#ff9500', yellow: '#ffcc00', green: '#34c759', blue: '#007aff',
      purple: '#af52de', pink: '#ff2d55', white: '#fff', black: '#000', gray: '#8e8e93',
      clear: 'transparent', primary: '#000', secondary: '#8e8e93', tertiary: '#8e8e93',
      quaternary: '#f2f2f7', accentColor: '#007aff', indigo: '#5856d6', mint: '#00c7be',
      teal: '#30b0c7', cyan: '#32ade6', brown: '#a2845e', background: '#fff',
    };
    return colors[String(name)] || String(name || 'transparent');
  }
  function children(parent, nodes, ctx, locals) {
    const scope = Object.assign(Object.create(null), locals);
    for (const node of nodes || []) {
      if (node.type === 'let') scope[node.name] = value(node.value, ctx, scope);
      else parent.appendChild(view(node, ctx, scope));
    }
  }
  function view(n, ctx, locals) {
    const ev = e => value(e, ctx, locals);
    let el;
    switch (n.type) {
      case 'component':
        for (const s of n.states) {
          const k = key(ctx, s.key);
          if (!state.has(k)) state.set(k, ev(s.value));
          else if (model.firebase?.readEnabled && s.key === model.firebase.stateKey) {
            try { state.set(k, restoreModel(state.get(k), model.firebase.type)); }
            catch {
              state.delete(k);
              state.set(k, ev(s.value));
              showRuntimeIssue('Saved browser data did not match this app version and was cleared.');
            }
          }
        }
        el = element('div', 'component');
        children(el, n.children, ctx, locals);
        break;
      case 'if':
        el = element('div', 'group');
        {
          const boundLocals = Object.assign(Object.create(null), locals);
          let bindingsSucceeded = true;
          for (const binding of n.bindings || []) {
            const bound = value(binding.value, ctx, boundLocals);
            if (bound == null) {
              bindingsSucceeded = false;
              break;
            }
            boundLocals[binding.name] = bound;
          }
          const branch = bindingsSucceeded && value(n.test, ctx, boundLocals) ? n.yes : n.no;
          const branchLocals = bindingsSucceeded && branch === n.yes ? boundLocals : locals;
          const start = el.children.length;
          children(el, branch, ctx, branchLocals);
          if (expressionChanged(n.test, ctx)) for (const child of Array.from(el.children).slice(start)) animateVisibleContents(child);
        }
        break;
      case 'switch': {
        el = element('div', 'group');
        const selected = ev(n.expression);
        const chosen = (n.cases || []).find(item => !item.default &&
          item.values.some(pattern => swiftEqual(selected, ev(pattern)))) || (n.cases || []).find(item => item.default);
        if (chosen) {
          const caseLocals = Object.assign(Object.create(null), locals);
          for (const binding of chosen.bindings || []) caseLocals[binding.name] = selected?.associated?.[binding.index] ?? null;
          children(el, chosen.children, ctx, caseLocals);
        }
        break;
      }
      case 'ForEach':
        el = element('div', 'group');
        ev(n.items).forEach((item, i) => {
          const rowLocals = {...locals, [n.key]: item};
          if ((n.params || []).length > 1) {
            const tuple = Array.isArray(item) ? item : [item?.offset, item?.element];
            n.params.forEach((parameter, index) => { rowLocals[parameter] = tuple[index]; });
          }
          children(el, n.children, ctx + '/' + n.key + ':' + i, rowLocals);
        });
        break;
      case 'List':
        el = element('ul', 'list');
        (ev(n.items) || []).forEach((item, i) => {
          const row = element('li', 'list-row');
          children(row, n.children, ctx + '/' + n.key + ':' + i, {...locals, [n.key]: item});
          el.appendChild(row);
        });
        break;
      case 'GroupBox':
        el = element('div', 'groupbox');
        if (n.label?.length) {
          const label = element('div', 'groupbox-label');
          children(label, n.label, ctx, locals);
          el.appendChild(label);
        }
        children(el, n.children, ctx, locals);
        break;
      case 'Section':
        el = element('section', 'form-section');
        if (n.header?.length) {
          const header = element('div', 'form-section-header');
          children(header, n.header, ctx, locals);
          el.appendChild(header);
        }
        const sectionBody = element('div', 'form-section-body');
        children(sectionBody, n.children, ctx, locals);
        el.appendChild(sectionBody);
        if (n.footer?.length) {
          const footer = element('div', 'form-section-footer');
          children(footer, n.footer, ctx, locals);
          el.appendChild(footer);
        }
        break;
      case 'LabeledContent':
        el = element('div', 'labeled-content');
        const fieldLabel = element('span', 'labeled-content-label');
        fieldLabel.textContent = String(ev(n.label) ?? '');
        const fieldValue = element('span', 'labeled-content-value');
        fieldValue.textContent = String(ev(n.value) ?? '');
        el.append(fieldLabel, fieldValue);
        break;
      case 'NavigationStack': {
        el = element('div', 'navigation-stack');
        const routes = Array.isArray(ev(n.path)) ? ev(n.path) : [];
        const destination = findNavigationDestination(n.children || []);
        if (routes.length && destination) {
          const back = element('button', 'back');
          back.type = 'button';
          back.textContent = '‹ Back';
          back.addEventListener('click', () => {
            writeBinding(n.path, routes.slice(0, -1), ctx, locals);
            persistState();
            render();
          });
          el.appendChild(back);
          children(el, destination.content, ctx, {...locals, [destination.param]: routes[routes.length - 1]});
        } else children(el, n.children, ctx, locals);
        break;
      }
      case 'Picker': {
        const selected = ev(n.binding);
        el = element('label', 'picker-control');
        if (n.label) {
          const title = element('span', 'picker-label');
          title.textContent = String(ev(n.label) ?? '');
          el.appendChild(title);
        }
        const holder = element('div');
        children(holder, n.children, ctx, locals);
        const select = element('select', 'picker');
        select.setAttribute('aria-label', String(ev(n.label) ?? 'Selection'));
        const options = Array.from(holder.querySelectorAll('[data-swift-tag]'));
        if (!options.length) options.push(...Array.from(holder.querySelectorAll('.text')));
        options.forEach((option, index) => {
          const item = element('option');
          const tagValue = Object.hasOwn(option.dataset, 'swiftTag') ? option.__swiftTagValue : option.textContent;
          item.value = String(index);
          item.textContent = option.textContent;
          select.appendChild(item);
          if (swiftEqual(tagValue, selected)) select.value = String(index);
          select.options[index].__swiftTagValue = tagValue;
        });
        select.addEventListener('change', () => {
          writeBinding(n.binding, select.options[select.selectedIndex]?.__swiftTagValue, ctx, locals);
          persistState();
          render();
        });
        el.appendChild(select);
        break;
      }
      case 'Stepper': {
        el = element('div', 'stepper');
        const label = element('span', 'stepper-label');
        label.textContent = String(ev(n.label) ?? '');
        const valueNow = Number(ev(n.binding) ?? 0);
        const minimum = Number(ev(n.min) ?? 0), maximum = Number(ev(n.max) ?? 0);
        const increment = Number(ev(n.step) || 1), upper = n.inclusiveMax ? maximum : maximum - increment;
        const decrement = element('button', 'stepper-button');
        decrement.type = 'button'; decrement.textContent = '−';
        decrement.setAttribute('aria-label', 'Decrease');
        decrement.disabled = valueNow <= minimum;
        decrement.addEventListener('click', () => {
          writeBinding(n.binding, Math.max(minimum, valueNow - increment), ctx, locals);
          persistState(); render();
        });
        const incrementButton = element('button', 'stepper-button');
        incrementButton.type = 'button'; incrementButton.textContent = '+';
        incrementButton.setAttribute('aria-label', 'Increase');
        incrementButton.disabled = valueNow >= upper;
        incrementButton.addEventListener('click', () => {
          writeBinding(n.binding, Math.min(upper, valueNow + increment), ctx, locals);
          persistState(); render();
        });
        el.append(label, decrement, incrementButton);
        break;
      }
      case 'Label':
        el = element('span', 'label-view');
        if (n.symbol) {
          const symbol = element('span', 'system-symbol');
          symbol.textContent = symbolGlyph(ev(n.symbol));
          symbol.setAttribute('aria-hidden', 'true');
          el.appendChild(symbol);
        }
        children(el, n.children, ctx, locals);
        break;
      case 'ContentUnavailableView':
        el = element('section', 'content-unavailable');
        if (n.symbol) {
          const symbol = element('span', 'system-symbol content-unavailable-symbol');
          symbol.textContent = symbolGlyph(ev(n.symbol));
          symbol.setAttribute('aria-hidden', 'true');
          el.appendChild(symbol);
        }
        {
          const title = element('div', 'content-unavailable-title');
          title.textContent = String(ev(n.title) ?? '');
          el.appendChild(title);
        }
        break;
      case 'Form':
        el = element('form', 'form-view');
        el.addEventListener('submit', event => event.preventDefault());
        children(el, n.children, ctx, locals);
        break;
      case 'LazyVGrid':
      case 'LazyHGrid':
        el = element('div', n.type === 'LazyVGrid' ? 'lazy-grid lazy-vgrid' : 'lazy-grid lazy-hgrid');
        children(el, n.children, ctx, locals);
        break;
      case 'ScrollViewReader':
      case 'TimelineView':
        el = element('div', 'group');
        children(el, n.children, ctx, locals);
        break;
      case 'Circle':
        el = element('div', 'circle');
        break;
      case 'Text':
        el = element('span', 'text');
        el.textContent = String(ev(n.text) ?? '');
        if (expressionChanged(n.text, ctx)) el.classList.add('swiftui-value-change');
        break;
      case 'Image':
        el = element('img', 'image');
        el.alt = String(ev(n.alt) ?? '');
        const imageSource = n.src || (n.assetName && model.assets?.[String(ev(n.assetName) ?? '')]);
        if (imageSource) el.src = imageSource;
        else {
          el = element('div', 'image image-missing');
          const message = `Missing image asset: ${String(ev(n.assetName) ?? ev(n.alt) ?? '')}`;
          el.setAttribute('role', 'img');
          el.setAttribute('aria-label', message);
          el.title = message;
          el.textContent = '?';
        }
        break;
      case 'Symbol':
        el = element('span', 'system-symbol');
        el.textContent = symbolGlyph(ev(n.symbolName));
        el.setAttribute('role', 'img');
        el.setAttribute('aria-label', String(ev(n.symbolName) ?? 'System symbol'));
        break;
      case 'missing-image':
        el = element('div', 'image-missing');
        el.setAttribute('role', 'img');
        el.setAttribute('aria-label', n.message);
        el.title = `${n.location}: ${n.message}`;
        el.textContent = '?';
        break;
      case 'Button':
        el = element('button', 'button');
        el.type = 'button';
        if (n.role === 'destructive') el.classList.add('destructive');
        children(el, n.children, ctx, locals);
        el.addEventListener('click', () => actions(n.actions, ctx, locals));
        break;
      case 'TextField':
      case 'SecureField': {
        el = element('input', 'text-field');
        el.type = n.type === 'SecureField' ? 'password' : 'text';
        el.placeholder = String(ev(n.label) ?? '');
        el.setAttribute('aria-label', el.placeholder || 'Text');
        el.value = String(ev(n.binding) ?? '');
        el.dataset.focus = JSON.stringify(bindingReference(n.binding));
        el.addEventListener('input', () => { writeBinding(n.binding, el.value, ctx, locals); persistState(); render(); });
        break;
      }
      case 'TextEditor': {
        el = element('textarea', 'text-editor');
        el.setAttribute('aria-label', 'Text');
        el.value = String(ev(n.binding) ?? '');
        el.dataset.focus = JSON.stringify(bindingReference(n.binding));
        el.addEventListener('input', () => { writeBinding(n.binding, el.value, ctx, locals); persistState(); render(); });
        break;
      }
      case 'DatePicker': {
        el = element('label', 'date-picker-control');
        const title = element('span', 'picker-label');
        title.textContent = String(ev(n.label) ?? 'Date');
        const input = element('input', 'date-picker');
        input.type = 'datetime-local';
        let selectedDate = ev(n.binding);
        if (!(selectedDate instanceof Date)) selectedDate = selectedDate ? new Date(selectedDate) : new Date();
        const asLocal = date => {
          const offset = date.getTimezoneOffset() * 60000;
          return new Date(date.getTime() - offset).toISOString().slice(0, 16);
        };
        input.value = Number.isNaN(selectedDate.valueOf()) ? asLocal(new Date()) : asLocal(selectedDate);
        input.dataset.focus = JSON.stringify(bindingReference(n.binding));
        input.addEventListener('change', () => {
          writeBinding(n.binding, new Date(input.value), ctx, locals);
          persistState(); render();
        });
        el.append(title, input);
        break;
      }
      case 'Toggle': {
        el = element('label', 'toggle');
        const input = element('input');
        input.type = 'checkbox';
        const toggleRef = bindingReference(n.binding);
        const toggleKey = toggleRef ? stateKey(ctx, toggleRef.key) : '';
        input.checked = Boolean(ev(n.binding));
        if (changedStateKeys.has(toggleKey)) input.classList.add('swiftui-toggle-change');
        input.dataset.focus = JSON.stringify(toggleRef);
        input.addEventListener('change', () => {
          writeBinding(n.binding, input.checked, ctx, locals);
          persistState();
          render();
        });
        if (n.children.length) children(el, n.children, ctx, locals);
        else el.appendChild(document.createTextNode(String(ev(n.label) ?? '')));
        el.appendChild(input);
        break;
      }
      case 'NavigationLink':
        el = element('button', 'navigation-link');
        el.type = 'button';
        children(el, n.children, ctx, locals);
        el.addEventListener('click', () => {
          pages.push({nodes: n.destination, ctx, locals, backTitle: document.querySelector('.navigation-title')?.textContent || ''});
          pageTransition = 'push';
          render();
        });
        break;
      case 'Spacer':
        el = element('div', 'spacer');
        el.setAttribute('aria-hidden', 'true');
        if (n.minLength) el.style.minHeight = el.style.minWidth = `${ev(n.minLength)}px`;
        break;
      case 'Divider': el = element('hr', 'divider'); break;
      case 'EmptyView': el = element('span'); el.hidden = true; break;
      case 'unsupported':
        el = element('div', 'unsupported');
        el.textContent = n.message;
        el.title = n.location;
        break;
      default:
        el = element('div', n.type.toLowerCase());
        if ((n.type === 'VStack' && hasFlexibleWidth(n)) || (n.type === 'HStack' && (hasFlexibleWidth(n) || hasSpacer(n)))) {
          el.style.width = '100%';
          el.style.alignSelf = 'stretch';
        }
        if (n.type === 'ScrollView' && n.axis === 'horizontal') el.classList.add('horizontal');
        if (n.gap) el.style.gap = `${ev(n.gap)}px`;
        if (n.align) el.style.alignItems = n.align;
        children(el, n.children, ctx, locals);
    }
    return modifiers(el, n.modifiers || [], ctx, locals);
  }
  function fillVerticalFrame(el) {
    el.style.flex = '1 1 auto';
    el.style.minHeight = '0';
    if (el.classList.contains('modifier')) {
      el.style.display = 'flex';
      el.style.flexDirection = 'column';
      el.style.alignItems = 'stretch';
      for (const child of el.children) fillVerticalFrame(child);
    }
  }
  function fillHorizontalFrame(el) {
    el.style.flex = '1 1 auto';
    el.style.minWidth = '0';
    if (el.classList.contains('modifier')) {
      el.style.display = 'flex';
      el.style.flexDirection = 'row';
      el.style.alignItems = 'stretch';
      for (const child of el.children) fillHorizontalFrame(child);
    }
  }
  function hasFlexibleWidth(node) {
    if (!node || typeof node !== 'object') return false;
    if (Array.isArray(node)) return node.some(hasFlexibleWidth);
    if (node.modifiers?.some(modifier => modifier.name === 'frame' && modifier.values?.maxWidth?.value === 'infinity')) return true;
    return Object.entries(node).some(([name, value]) => name !== 'src' && name !== 'assets' && hasFlexibleWidth(value));
  }
  function hasSpacer(node) {
    if (!node || typeof node !== 'object') return false;
    if (Array.isArray(node)) return node.some(hasSpacer);
    if (node.type === 'Spacer') return true;
    return Object.entries(node).some(([name, value]) => name !== 'src' && name !== 'assets' && hasSpacer(value));
  }
  function modifiers(initial, mods, ctx, locals) {
    let el = initial;
    const verticalContent = initial.classList?.contains('vstack') || initial.classList?.contains('scrollview');
    const horizontalContent = initial.classList?.contains('hstack');
    for (const m of mods) {
      const v = value(m.value, ctx, locals);
      // Wrappers preserve SwiftUI modifier ordering for layout and decoration.
      const content = el;
      if (['padding', 'frame', 'background', 'cornerRadius', 'opacity'].includes(m.name)) {
        const wrapper = element('div', 'modifier');
        if (el.style.width === '100%' || el.style.alignSelf === 'stretch') {
          wrapper.style.width = '100%';
          wrapper.style.alignSelf = 'stretch';
        }
        wrapper.appendChild(el);
        el = wrapper;
      }
      switch (m.name) {
        case 'tag':
          el.dataset.swiftTag = 'true';
          el.__swiftTagValue = v;
          break;
        case 'padding': {
          const px = `${v}px`;
          if (m.edge === 'all') el.style.padding = px;
          if (m.edge === 'horizontal') el.style.paddingInline = px;
          if (m.edge === 'vertical') el.style.paddingBlock = px;
          if (m.edge === 'top') el.style.paddingTop = px;
          if (m.edge === 'bottom') el.style.paddingBottom = px;
          if (m.edge === 'leading') el.style.paddingInlineStart = px;
          if (m.edge === 'trailing') el.style.paddingInlineEnd = px;
          break;
        }
        case 'frame':
          for (const [prop, expression] of Object.entries(m.values)) {
            const x = value(expression, ctx, locals);
            el.style[prop] = x === 'infinity' ? (prop === 'maxWidth' ? '100%' : 'none') : `${x}px`;
            if (prop === 'maxWidth' && x === 'infinity') {
              el.style.width = '100%';
              if (verticalContent) {
                el.style.flexDirection = 'column';
                el.style.alignItems = 'stretch';
              }
            }
          }
          if (m.alignment) {
            el.style.display = 'flex';
            if (verticalContent) {
              el.style.flexDirection = 'column';
              el.style.alignItems = 'stretch';
              if (m.alignment === 'top') el.style.justifyContent = 'flex-start';
              if (m.alignment === 'bottom') el.style.justifyContent = 'flex-end';
            } else {
              el.style.justifyContent = ({leading: 'flex-start', trailing: 'flex-end'})[m.alignment] || 'center';
              el.style.alignItems = ({top: 'flex-start', bottom: 'flex-end'})[m.alignment] || 'center';
            }
          }
          const horizontalSize = 'width' in m.values || 'minWidth' in m.values || 'maxWidth' in m.values;
          const verticalSize = 'height' in m.values || 'minHeight' in m.values || 'maxHeight' in m.values;
          if (verticalContent && (horizontalSize || verticalSize)) {
            el.style.display = 'flex';
            el.style.flexDirection = 'column';
            el.style.alignItems = 'stretch';
            fillVerticalFrame(content);
          }
          if (horizontalContent && (horizontalSize || verticalSize)) {
            el.style.display = 'flex';
            el.style.flexDirection = 'row';
            el.style.alignItems = 'stretch';
            fillHorizontalFrame(content);
          }
          break;
        case 'background':
          el.style.backgroundColor = cssColor(value(m.value, ctx, locals));
          if (m.shape === 'Capsule') {
            el.style.borderRadius = '9999px';
            el.style.overflow = 'hidden';
          } else if (m.shape === 'RoundedRectangle') {
            el.style.borderRadius = `${value(m.radius, ctx, locals)}px`;
            el.style.overflow = 'hidden';
          }
          break;
        case 'foregroundColor':
        case 'foregroundStyle': el.style.color = cssColor(value(m.value, ctx, locals)); break;
        case 'backgroundStyle': el.style.setProperty('--swiftui-background-style', cssColor(value(m.value, ctx, locals))); break;
        case 'tint': el.style.setProperty('--tint', cssColor(value(m.value, ctx, locals))); el.style.accentColor = cssColor(value(m.value, ctx, locals)); break;
        case 'font': el.style.fontSize = `${v}px`; el.style.fontWeight = m.weight; break;
        case 'multilineTextAlignment': el.style.textAlign = m.value; break;
        case 'listStyle': el.classList.add(`list-style-${m.value}`); break;
        case 'fontWeight': el.style.fontWeight = m.value; break;
        case 'bold': el.style.fontWeight = '700'; break;
        case 'italic': el.style.fontStyle = 'italic'; break;
        case 'cornerRadius': el.style.borderRadius = `${v}px`; el.style.overflow = 'hidden'; break;
        case 'opacity': el.style.opacity = String(v); break;
        case 'resizable': el.style.width = '100%'; el.style.height = '100%'; break;
        case 'scaledToFit': el.style.objectFit = 'contain'; break;
        case 'scaledToFill': el.style.objectFit = 'cover'; break;
        case 'fixedSize':
          if (m.horizontal) el.style.width = 'max-content';
          if (m.vertical) el.style.height = 'max-content';
          el.style.flex = '0 0 auto';
          break;
        case 'disabled':
          for (const control of [el, ...el.querySelectorAll('button, input')]) {
            if ('disabled' in control && v) control.disabled = true;
          }
          break;
        case 'navigationTitle': {
          const heading = element('h1', 'navigation-title');
          heading.textContent = String(v ?? '');
          const wrapper = element('section', 'navigation-section');
          wrapper.append(heading, el);
          el = wrapper;
          break;
        }
        case 'accessibilityLabel': el.setAttribute('aria-label', String(v ?? '')); break;
        case 'lineLimit': {
          const count = Number(v);
          if (Number.isFinite(count) && count > 0) {
            el.style.display = '-webkit-box';
            el.style.webkitBoxOrient = 'vertical';
            el.style.webkitLineClamp = String(Math.trunc(count));
            el.style.overflow = 'hidden';
          }
          break;
        }
        case 'minimumScaleFactor': el.style.setProperty('--minimum-scale-factor', String(v)); break;
        case 'strikethrough': if (v) el.style.textDecoration = 'line-through'; break;
        case 'buttonStyle': el.classList.add(`button-style-${String(m.value?.value || '').replace(/^\./, '')}`); break;
        case 'pickerStyle': el.classList.add(`picker-style-${String(m.value?.value || '').replace(/^\./, '')}`); break;
        case 'textFieldStyle': el.classList.add(`text-field-style-${String(m.value?.value || '').replace(/^\./, '')}`); break;
        case 'toolbar': {
          const host = element('div', 'toolbar-host');
          const bar = element('div', 'swiftui-toolbar');
          children(bar, m.content, ctx, locals);
          host.append(bar, el);
          el = host;
          break;
        }
        case 'sheet':
        case 'confirmationDialog': {
          const presentedValue = readBinding(m.binding, ctx, locals);
          const isPresented = m.showMode === 'nonNil' ? presentedValue != null
            : m.showMode === 'nil' ? presentedValue == null : Boolean(presentedValue);
          if (isPresented) {
            const overlay = element('div', m.name === 'sheet' ? 'sheet-overlay' : 'dialog-overlay');
            const panel = element('section', m.name === 'sheet' ? 'sheet-panel' : 'confirmation-panel');
            panel.setAttribute('role', m.name === 'sheet' ? 'dialog' : 'alertdialog');
            panel.setAttribute('aria-modal', 'true');
            const dismiss = () => {
              writeBinding(m.binding, m.showMode === 'truthy' ? false : null, ctx, locals);
              persistState();
              render();
            };
            overlay.addEventListener('click', event => { if (event.target === overlay) dismiss(); });
            panel.addEventListener('click', event => event.stopPropagation());
            const close = element('button', 'sheet-close');
            close.type = 'button'; close.textContent = '×'; close.setAttribute('aria-label', 'Close');
            close.addEventListener('click', dismiss);
            panel.appendChild(close);
            if (m.name === 'sheet') {
              const scope = Object.assign(Object.create(null), locals);
              scope.__swiftwebDismiss = dismiss;
              if (m.param) scope[m.param] = presentedValue;
              children(panel, m.content, ctx, scope);
            } else {
              const title = element('h2', 'confirmation-title');
              title.textContent = String(value(m.title, ctx, locals) ?? '');
              panel.appendChild(title);
              const message = element('div', 'confirmation-message');
              children(message, m.message || [], ctx, locals);
              panel.appendChild(message);
              const buttons = element('div', 'confirmation-buttons');
              children(buttons, m.buttons || [], ctx, locals);
              panel.appendChild(buttons);
            }
            const host = element('div', 'presentation-host');
            host.append(el, overlay);
            el = host;
          }
          break;
        }
      }
    }
    return el;
  }
  function render() {
    const active = document.activeElement;
    const focus = active?.dataset.focus;
    const selection = active?.type === 'text' || active?.type === 'password' ? [active.selectionStart, active.selectionEnd] : null;
    app.replaceChildren();
    const currentPage = pages[pages.length - 1];
    const transition = pageTransition;
    pageTransition = '';
    if (pages.length > 1) {
      const back = element('button', 'back');
      back.textContent = `‹ ${currentPage.backTitle || 'Back'}`;
      back.type = 'button';
      back.addEventListener('click', () => {pages.pop(); pageTransition = 'pop'; render();});
      app.appendChild(back);
    }
    const page = element('div', `navigation-page${transition ? ` swiftui-navigation-${transition}` : ''}`);
    children(page, currentPage.nodes, currentPage.ctx, currentPage.locals);
    app.appendChild(page);
    changedStateKeys.clear();
    if (focus) {
      const input = Array.from(app.querySelectorAll('[data-focus]')).find(x => x.dataset.focus === focus);
      if (input) {
        input.focus();
        if (selection) input.setSelectionRange(...selection);
      }
    }
  }
  const runtimeNotices = new Set();
  function showRuntimeIssue(message) {
    if (runtimeNotices.has(message)) return;
    runtimeNotices.add(message);
    const li = element('li', 'runtime-warning');
    li.textContent = message;
    document.getElementById('report-items').appendChild(li);
  }
  function arrayValue(raw) {
    if (Array.isArray(raw)) return raw;
    if (!raw || typeof raw !== 'object') throw new Error('Expected an array');
    const keys = Object.keys(raw);
    if (!keys.every(key => /^\d+$/.test(key))) throw new Error('Expected array indexes');
    const sorted = keys.sort((a, b) => Number(a) - Number(b));
    if (sorted.some((key, index) => Number(key) !== index)) throw new Error('Expected contiguous array indexes');
    return sorted.map(key => raw[key]);
  }
  function restoreModel(raw, typeName, optional = false) {
    if (raw == null) {
      if (optional) return null;
      throw new Error(`Expected ${typeName}`);
    }
    if (typeName === 'String' && typeof raw !== 'string') throw new Error('Expected String');
    if (typeName === 'Bool' && typeof raw !== 'boolean') throw new Error('Expected Bool');
    if (typeName === 'Int' && !Number.isInteger(raw)) throw new Error('Expected Int');
    if (['Double', 'Float'].includes(typeName) && (typeof raw !== 'number' || !Number.isFinite(raw))) throw new Error(`Expected ${typeName}`);
    const fields = model.modelFields?.[typeName];
    if (!fields) return raw;
    if (typeof raw !== 'object' || Array.isArray(raw)) throw new Error(`Expected ${typeName} object`);
    if (raw.__type && raw.__type !== typeName) throw new Error(`Expected ${typeName} value`);
    const result = {__type: typeName};
    for (const [name, spec] of Object.entries(fields)) {
      if (!Object.hasOwn(raw, name)) {
        if (spec.optional) { result[name] = null; continue; }
        throw new Error(`Missing ${typeName}.${name}`);
      }
      if (spec.array) result[name] = arrayValue(raw[name]).map(item => restoreModel(item, spec.array));
      else result[name] = restoreModel(raw[name], spec.type, Boolean(spec.optional));
    }
    return result;
  }
  function plainModel(raw) {
    if (Array.isArray(raw)) return raw.map(plainModel);
    if (raw && typeof raw === 'object') {
      return Object.fromEntries(Object.entries(raw).filter(([name]) => name !== '__type').map(([name, item]) => [name, plainModel(item)]));
    }
    return raw;
  }
  function firebaseUrl(path) {
    const parts = String(path ?? '').split('/').filter(Boolean).map(encodeURIComponent);
    return `${model.firebase.baseUrl}/${parts.join('/')}.json`;
  }
  async function firebaseRequest(path, method = 'GET', body = undefined, headers = {}) {
    if (firebaseDisabled) throw new Error('Firebase access is disabled in the URL');
    const verb = String(method || 'GET').toUpperCase();
    if (!['GET', 'PUT', 'POST', 'PATCH', 'DELETE'].includes(verb)) throw new Error(`Firebase method ${verb} is not allowed`);
    if (verb !== 'GET' && !model.firebase?.writeEnabled) throw new Error('Firebase writes are not enabled for this translated app');
    const requestHeaders = new Headers(headers || {});
    if (body !== undefined && body !== null && verb !== 'DELETE') requestHeaders.set('Content-Type', 'application/json');
    const response = await fetch(firebaseUrl(path), {
      method: verb,
      cache: 'no-store',
      headers: requestHeaders,
      ...(body !== undefined && body !== null && verb !== 'DELETE' ? {body: JSON.stringify(plainModel(body))} : {}),
    });
    const text = await response.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); }
      catch { data = text; }
    }
    if (!response.ok) {
      const error = new Error(typeof data === 'string' ? data : data?.error || `HTTP ${response.status}`);
      error.statusCode = response.status;
      error.path = path;
      error.swiftType = 'FirebaseHTTPError';
      throw error;
    }
    return {data, etag: response.headers.get('ETag'), status: response.status};
  }
  async function firebaseRead(path) { return (await firebaseRequest(path, 'GET')).data; }
  async function firebaseReadSnapshot(path) {
    const {data, etag} = await firebaseRequest(path, 'GET', undefined, {'X-Firebase-ETag': 'true'});
    return {value: data, etag};
  }
  async function firebaseWrite(path, data) {
    const result = await firebaseRequest(path, 'PUT', data);
    return result.data;
  }
  if (model.firebase && !firebaseDisabled) {
    const connector = {request: firebaseRequest};
    if (model.firebase.readEnabled) {
      connector.read = firebaseRead;
      connector.readSnapshot = firebaseReadSnapshot;
    }
    if (model.firebase.writeEnabled) {
      connector.write = async (path, data, options = {}) => {
        const result = await firebaseRequest(path, options.method || 'PUT', data, options.headers || {});
        return result.data;
      };
    }
    window.swiftToWebFirebase = Object.freeze(connector);
  }
  const report = document.getElementById('report-items');
  for (const d of model.diagnostics) {
    const li = element('li');
    li.textContent = `${d.file}:${d.line} — ${d.message}`;
    report.appendChild(li);
  }
  document.getElementById('report-summary').textContent = `Conversion report (${model.diagnostics.length} warning${model.diagnostics.length === 1 ? '' : 's'})`;
  if (!model.diagnostics.length) report.textContent = 'No unsupported features detected in the parsed subset.';
  render();
  if (model.firebase?.readEnabled && !firebaseDisabled) {
    let syncing = false;
    const syncFirebase = async () => {
      if (syncing) return;
      syncing = true;
      try {
        let data;
        try { data = await firebaseRead(model.firebase.path); }
        catch {
          showRuntimeIssue('Firebase could not load data. Check the network, database URL, and read access rules.');
          return;
        }
        if (data !== null) {
          const stateName = stateKey('', model.firebase.stateKey);
          try { state.set(stateName, restoreModel(data, model.firebase.type)); }
          catch {
            showRuntimeIssue('Firebase returned data that does not match this Swift model; cached values were kept.');
            return;
          }
          changedStateKeys.add(stateName);
          persistState();
          render();
        }
      } finally {
        syncing = false;
      }
    };
    syncFirebase();
    window.setInterval(syncFirebase, model.firebase.refreshMs || 10000);
  }
})();
