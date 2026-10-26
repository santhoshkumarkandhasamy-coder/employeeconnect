import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../core/api/api_exception.dart';
import '../../../../core/security/screen_protect.dart';
import '../../../admin/domain/admin_models.dart' show adminErrorMessage;
import '../../../admin/presentation/admin_widgets.dart';
import '../../data/on_duty_repository.dart';
import '../../domain/duty_admin_models.dart';
import 'duty_admin_providers.dart';
import 'duty_assignments_screen.dart';
import 'duty_location_form_screen.dart';

/// Admin > On-duty locations (`duty_location.manage`). [readPosition] enables
/// "Use my current location" in the form (phone app only).
class DutyLocationsScreen extends ConsumerStatefulWidget {
  const DutyLocationsScreen({super.key, this.readPosition});
  final CurrentPositionReader? readPosition;

  @override
  ConsumerState<DutyLocationsScreen> createState() => _LocationsState();
}

class _LocationsState extends ConsumerState<DutyLocationsScreen> {
  String _q = '';
  bool? _active;

  DutyLocationQuery get _query => DutyLocationQuery(q: _q, isActive: _active);

  Future<void> _edit(DutyLocationAdmin? loc) async {
    final saved = await Navigator.of(context).push<bool>(
      MaterialPageRoute<bool>(
        builder: (_) => DutyLocationFormScreen(existing: loc, readPosition: widget.readPosition),
      ),
    );
    if (saved == true) ref.invalidate(dutyLocationsAdminProvider(_query));
  }

  Future<void> _assign(DutyLocationAdmin loc) async {
    final saved = await Navigator.of(context).push<bool>(
      MaterialPageRoute<bool>(builder: (_) => DutyAssignmentsScreen(location: loc)),
    );
    if (saved == true) ref.invalidate(dutyLocationsAdminProvider(_query));
  }

  Future<void> _delete(DutyLocationAdmin loc) async {
    final ok = await confirmAction(
      context,
      title: 'Delete ${loc.name}?',
      message: loc.inUse
          ? 'This location has visits or requests, so it cannot be deleted. Deactivate it instead.'
          : 'This cannot be undone.',
      confirmLabel: 'Delete',
    );
    if (!ok || !mounted) return;
    try {
      await ref.read(onDutyRepositoryProvider).deleteLocation(loc.id);
      ref.invalidate(dutyLocationsAdminProvider(_query));
      if (mounted) showAdminSnack(context, 'Deleted.');
    } on ApiException catch (e) {
      if (mounted) showAdminSnack(context, adminErrorMessage(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final perms = ref.watch(adminPermissionsProvider);
    if (!canManageDutyLocations(perms)) return const NoPermissionScreen(title: 'On-duty locations');
    final async = ref.watch(dutyLocationsAdminProvider(_query));
    final notifier = ref.read(dutyLocationsAdminProvider(_query).notifier);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(title: const Text('On-duty locations')),
        floatingActionButton: FloatingActionButton.extended(
          onPressed: () => _edit(null),
          icon: const Icon(Icons.add),
          label: const Text('Add location'),
        ),
        body: AdminPagedBody<DutyLocationAdmin>(
          async: async,
          onRefresh: notifier.reload,
          onLoadMore: notifier.loadMore,
          emptyMessage: _q.isEmpty && _active == null
              ? 'No on-duty locations yet. Tap Add location to create one.'
              : 'No locations match.',
          emptyIcon: Icons.place_outlined,
          header: Padding(
            padding: const EdgeInsets.fromLTRB(12, 12, 12, 0),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                TextField(
                  decoration: const InputDecoration(
                    labelText: 'Search name or address',
                    prefixIcon: Icon(Icons.search),
                    border: OutlineInputBorder(),
                  ),
                  textInputAction: TextInputAction.search,
                  onSubmitted: (v) => setState(() => _q = v.trim()),
                ),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 8,
                  children: [
                    for (final f in <(String, bool?)>[('All', null), ('Active', true), ('Inactive', false)])
                      ChoiceChip(
                        label: Text(f.$1),
                        selected: _active == f.$2,
                        onSelected: (_) => setState(() => _active = f.$2),
                      ),
                  ],
                ),
              ],
            ),
          ),
          itemBuilder: (context, loc) => Card(
            margin: const EdgeInsets.symmetric(vertical: 4),
            child: ListTile(
              enabled: loc.isActive,
              isThreeLine: true,
              title: Text(loc.name),
              subtitle: Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    if (loc.address != null) Text(loc.address!, maxLines: 2, overflow: TextOverflow.ellipsis),
                    Text('Radius ${loc.radiusM} m${loc.isActive ? '' : '  -  Inactive'}'),
                    Wrap(
                      spacing: 8,
                      children: [
                        Chip(
                          label: Text(loc.assignText),
                          visualDensity: VisualDensity.compact,
                          avatar: Icon(loc.assignsAll ? Icons.groups_outlined : Icons.person_outline, size: 16),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
              onTap: () => _edit(loc),
              trailing: PopupMenuButton<String>(
                tooltip: 'Actions',
                onSelected: (v) => switch (v) {
                  'edit' => _edit(loc),
                  'assign' => _assign(loc),
                  _ => _delete(loc),
                },
                itemBuilder: (_) => const [
                  PopupMenuItem(value: 'edit', child: Text('Edit')),
                  PopupMenuItem(value: 'assign', child: Text('Assignments')),
                  PopupMenuItem(value: 'delete', child: Text('Delete')),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
