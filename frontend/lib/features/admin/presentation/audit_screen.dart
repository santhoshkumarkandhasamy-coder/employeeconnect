import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/security/screen_protect.dart';
import '../../../core/utils/dates.dart';
import '../domain/admin_models.dart';
import 'admin_providers.dart';
import 'admin_widgets.dart';

/// Read-only, filterable audit log viewer (`audit.view`). Rows can hold
/// personal data (masked server side), so it is a [SecureScreen].
class AuditScreen extends ConsumerStatefulWidget {
  const AuditScreen({super.key});

  @override
  ConsumerState<AuditScreen> createState() => _AuditScreenState();
}

class _AuditScreenState extends ConsumerState<AuditScreen> {
  AuditQuery _query = const AuditQuery();

  Future<void> _filter() async {
    final q = await showDialog<AuditQuery>(
      context: context,
      builder: (_) => AuditFilterDialog(initial: _query),
    );
    if (q != null) setState(() => _query = q);
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!perms.contains(AdminPerm.auditView)) return const NoPermissionScreen(title: 'Audit log');
    final async = ref.watch(auditLogsProvider(_query));
    final notifier = ref.read(auditLogsProvider(_query).notifier);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: const Text('Audit log'),
          actions: [
            IconButton(
              tooltip: 'Filters',
              icon: Badge(isLabelVisible: !_query.isEmpty, child: const Icon(Icons.filter_list)),
              onPressed: _filter,
            ),
          ],
        ),
        body: AdminPagedBody<AuditEntry>(
          async: async,
          onRefresh: notifier.reload,
          onLoadMore: notifier.loadMore,
          emptyIcon: Icons.fact_check_outlined,
          emptyMessage: _query.isEmpty ? 'No audit entries yet.' : 'No entries match these filters.',
          itemBuilder: (context, a) => Card(
            margin: const EdgeInsets.symmetric(vertical: 4),
            child: ListTile(
              title: Text(a.action),
              subtitle: Text([
                a.actor?.display ?? 'System',
                if (a.entityType.isNotEmpty) a.entityType,
                if (a.ts != null) '${ymd(a.ts!)} ${formatTime(a.ts!)}',
              ].join('  -  '),),
              trailing: a.outcome.isEmpty ? null : Chip(label: Text(a.outcome)),
              onTap: () => Navigator.of(context).push<void>(
                MaterialPageRoute<void>(builder: (_) => AuditDetailScreen(entry: a)),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class AuditFilterDialog extends StatefulWidget {
  const AuditFilterDialog({required this.initial, super.key});
  final AuditQuery initial;

  @override
  State<AuditFilterDialog> createState() => _AuditFilterDialogState();
}

class _AuditFilterDialogState extends State<AuditFilterDialog> {
  late final _actor = TextEditingController(text: widget.initial.actor);
  late final _action = TextEditingController(text: widget.initial.action);
  late final _entityType = TextEditingController(text: widget.initial.entityType);
  late final _entityId = TextEditingController(text: widget.initial.entityId);
  late String? _from = widget.initial.from;
  late String? _to = widget.initial.to;
  String? _rangeError;

  @override
  void dispose() {
    for (final c in [_actor, _action, _entityType, _entityId]) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _pickDate({required bool from}) async {
    final now = DateTime.now();
    final d = await showDatePicker(
      context: context,
      initialDate: parseYmd(from ? _from : _to) ?? now,
      firstDate: DateTime(now.year - 5),
      lastDate: DateTime(now.year + 1),
    );
    if (d != null) {
      setState(() {
        if (from) {
          _from = ymd(d);
        } else {
          _to = ymd(d);
        }
        _rangeError = validateAuditRange(_from, _to);
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    InputDecoration dec(String l) => InputDecoration(labelText: l, border: const OutlineInputBorder(), isDense: true);
    return AlertDialog(
      title: const Text('Filter audit log'),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(controller: _actor, decoration: dec('Actor (user id or employee ID)')),
            const SizedBox(height: 10),
            TextField(controller: _action, decoration: dec('Action (exact, or prefix like employee)')),
            const SizedBox(height: 10),
            TextField(controller: _entityType, decoration: dec('Entity type')),
            const SizedBox(height: 10),
            TextField(controller: _entityId, decoration: dec('Entity id')),
            const SizedBox(height: 10),
            Row(
              children: [
                Expanded(
                  child: OutlinedButton(
                    onPressed: () => _pickDate(from: true),
                    child: Text(_from == null ? 'From (UTC)' : 'From $_from'),
                  ),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: OutlinedButton(
                    onPressed: () => _pickDate(from: false),
                    child: Text(_to == null ? 'To (UTC)' : 'To $_to'),
                  ),
                ),
              ],
            ),
            if (_rangeError != null)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(_rangeError!, style: TextStyle(color: Theme.of(context).colorScheme.error)),
              ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context, const AuditQuery()),
          child: const Text('Clear'),
        ),
        FilledButton(
          onPressed: _rangeError != null
              ? null
              : () => Navigator.pop(
                    context,
                    AuditQuery(
                      actor: _actor.text.trim(),
                      action: _action.text.trim(),
                      entityType: _entityType.text.trim(),
                      entityId: _entityId.text.trim(),
                      from: _from,
                      to: _to,
                    ),
                  ),
          style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
          child: const Text('Apply'),
        ),
      ],
    );
  }
}

class AuditDetailScreen extends StatelessWidget {
  const AuditDetailScreen({required this.entry, super.key});
  final AuditEntry entry;

  @override
  Widget build(BuildContext context) {
    Widget block(String title, String body) => Padding(
          padding: const EdgeInsets.only(bottom: 16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(title, style: Theme.of(context).textTheme.titleSmall),
              const SizedBox(height: 4),
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: Theme.of(context).colorScheme.surfaceContainerHighest,
                  borderRadius: BorderRadius.circular(8),
                ),
                child: SelectableText(body, style: const TextStyle(fontFamily: 'monospace', fontSize: 12)),
              ),
            ],
          ),
        );
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: Text(entry.action)),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            block('Who', entry.actor?.display ?? 'System'),
            block('When', entry.ts == null ? '-' : '${ymd(entry.ts!)} ${formatTime(entry.ts!)}'),
            block('Entity', '${entry.entityType} ${entry.entityId ?? ''}'.trim()),
            block('Outcome', entry.outcome.isEmpty ? '-' : entry.outcome),
            if (entry.ip != null) block('IP address', entry.ip!),
            block('Before', prettyJson(entry.before)),
            block('After', prettyJson(entry.after)),
          ],
        ),
      ),
    );
  }
}
