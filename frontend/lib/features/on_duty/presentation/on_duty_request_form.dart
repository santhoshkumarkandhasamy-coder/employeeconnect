import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/utils/dates.dart';
import '../../../core/widgets/state_views.dart';
import '../../leave/domain/leave_logic.dart' show leaveErrorMessage;
import '../../leave/presentation/widgets/form_widgets.dart';
import '../data/on_duty_repository.dart';
import '../domain/on_duty_request_logic.dart';
import 'on_duty_providers.dart';

/// Apply for On Duty: date range, one site or "any assigned site", purpose.
/// Allowed whether or not approval is enforced (contract 14.3).
class OnDutyRequestForm extends ConsumerStatefulWidget {
  const OnDutyRequestForm({required this.onSubmitted, super.key, this.today});
  final VoidCallback onSubmitted;

  /// Test seam for the current date.
  final DateTime? today;

  @override
  ConsumerState<OnDutyRequestForm> createState() => _OnDutyRequestFormState();
}

class _OnDutyRequestFormState extends ConsumerState<OnDutyRequestForm> {
  final _formKey = GlobalKey<FormState>();
  final _purpose = TextEditingController();
  DateTime? _from;
  DateTime? _to;

  /// null = any assigned location.
  String? _locationId;
  bool _busy = false;
  String? _error;

  DateTime get _today {
    final t = widget.today ?? DateTime.now();
    return DateTime(t.year, t.month, t.day);
  }

  @override
  void dispose() {
    _purpose.dispose();
    super.dispose();
  }

  Future<void> _pick({required bool from}) async {
    final cfg = ref.read(onDutyConfigProvider);
    final first = earliestOnDutyFrom(_today, cfg.requestBackdateDays);
    final last = latestOnDutyTo(_today, cfg.requestMaxAdvanceDays);
    final init = (from ? _from : _to) ?? _from ?? _today;
    final d = await showDatePicker(
      context: context,
      initialDate: init.isBefore(first) ? first : (init.isAfter(last) ? last : init),
      firstDate: first,
      lastDate: last,
      helpText: from ? 'From date' : 'To date',
    );
    if (d == null) return;
    setState(() {
      if (from) {
        _from = d;
        if (_to == null || _to!.isBefore(d)) _to = d;
      } else {
        _to = d;
      }
      _error = null;
    });
  }

  Future<void> _submit() async {
    if (_busy) return;
    if (!(_formKey.currentState?.validate() ?? false)) return;
    final cfg = ref.read(onDutyConfigProvider);
    final err = validateOnDutyRequest(
      from: _from,
      to: _to,
      purpose: _purpose.text,
      today: _today,
      backdateDays: cfg.requestBackdateDays,
      maxAdvanceDays: cfg.requestMaxAdvanceDays,
    );
    if (err != null) {
      setState(() => _error = err);
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(onDutyRepositoryProvider).applyRequest(
            from: _from!,
            to: _to!,
            locationId: _locationId,
            purpose: _purpose.text,
          );
    } on ApiException catch (e) {
      if (e.code == 'DUTY_LOCATION_UNAVAILABLE') {
        ref.invalidate(dutyLocationsProvider);
        _locationId = null;
      }
      if (!mounted) return;
      setState(() {
        _busy = false;
        _error = leaveErrorMessage(e);
      });
      return;
    }
    refreshOnDutyRequests(ref);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('On duty request submitted.')));
    _formKey.currentState?.reset();
    _purpose.clear();
    setState(() {
      _busy = false;
      _from = _to = null;
      _locationId = null;
    });
    widget.onSubmitted();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final cfg = ref.watch(onDutyConfigProvider);
    final sites = ref.watch(dutyLocationsProvider);
    final days = _from != null && _to != null && !_to!.isBefore(_from!) ? onDutyDays(_from!, _to!) : null;
    return Form(
      key: _formKey,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (_error != null) FormErrorBox(_error!),
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Text(
              cfg.approvalRequired
                  ? 'Your company needs an approved On Duty request before you can check in at a site.'
                  : 'Optional: tell your approver where you will be. You can check in without approval.',
              style: theme.textTheme.bodySmall,
            ),
          ),
          Row(
            children: [
              Expanded(
                child: PickerField(
                  label: 'From',
                  value: _from == null ? null : formatDayLong(_from!),
                  icon: Icons.calendar_today_outlined,
                  onTap: _busy ? null : () => _pick(from: true),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: PickerField(
                  label: 'To',
                  value: _to == null ? null : formatDayLong(_to!),
                  icon: Icons.calendar_today_outlined,
                  onTap: _busy ? null : () => _pick(from: false),
                ),
              ),
            ],
          ),
          if (days != null)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(
                '$days calendar ${days == 1 ? 'day' : 'days'} selected.',
                style: theme.textTheme.bodySmall?.copyWith(
                  color: days > maxOnDutySpanDays ? theme.colorScheme.error : null,
                ),
              ),
            ),
          const SizedBox(height: 12),
          sites.when(
            loading: () => const Padding(padding: EdgeInsets.all(8), child: LoadingView(label: 'Loading sites')),
            error: (e, _) => ErrorView(
              message: e is ApiException ? e.message : 'Could not load your sites.',
              onRetry: () => ref.invalidate(dutyLocationsProvider),
            ),
            data: (list) => DropdownButtonFormField<String?>(
              initialValue: list.any((s) => s.id == _locationId) ? _locationId : null,
              isExpanded: true,
              decoration: const InputDecoration(labelText: 'Location', border: OutlineInputBorder()),
              items: [
                const DropdownMenuItem<String?>(child: Text('Any assigned location')),
                for (final s in list)
                  DropdownMenuItem<String?>(
                    value: s.id,
                    child: Text(s.name, overflow: TextOverflow.ellipsis),
                  ),
              ],
              onChanged: _busy ? null : (v) => setState(() => _locationId = v),
            ),
          ),
          const SizedBox(height: 12),
          TextFormField(
            controller: _purpose,
            enabled: !_busy,
            maxLines: 3,
            maxLength: maxPurposeLength,
            textInputAction: TextInputAction.newline,
            decoration: const InputDecoration(labelText: 'Purpose', border: OutlineInputBorder()),
            validator: validatePurpose,
          ),
          const SizedBox(height: 8),
          FilledButton(
            onPressed: _busy ? null : _submit,
            style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(48)),
            child: _busy
                ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('Submit on duty request'),
          ),
        ],
      ),
    );
  }
}

/// Full-screen wrapper (opened from "Request On Duty" on the Today tab).
class OnDutyRequestScreen extends StatelessWidget {
  const OnDutyRequestScreen({super.key});

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('Request On Duty')),
        body: ListView(
          padding: const EdgeInsets.all(16),
          children: [OnDutyRequestForm(onSubmitted: () => Navigator.of(context).maybePop())],
        ),
      );
}
