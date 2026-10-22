import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/utils/dates.dart';
import '../domain/profile_models.dart';

/// Edit form for the employee's own profile. Validates like the server
/// (plan 07), builds a changed-keys-only body, and reports whether anything
/// differs from the saved profile ([onDirtyChanged]) for the discard guard.
class ProfileEditForm extends ConsumerStatefulWidget {
  const ProfileEditForm({
    required this.profile,
    required this.onSave,
    required this.onCancel,
    super.key,
    this.onDirtyChanged,
    this.clock,
  });

  final Profile profile;

  /// Returns an error message, or null when saved.
  final Future<String?> Function(Map<String, Object?> body) onSave;
  final VoidCallback onCancel;

  final ValueChanged<bool>? onDirtyChanged;
  final DateTime Function()? clock;

  @override
  ConsumerState<ProfileEditForm> createState() => _ProfileEditFormState();
}

class _ProfileEditFormState extends ConsumerState<ProfileEditForm> {
  final _formKey = GlobalKey<FormState>();
  late final TextEditingController _dob;
  late final TextEditingController _anniv;
  late final TextEditingController _personalEmail;
  late final TextEditingController _phone;
  late final TextEditingController _emName;
  late final TextEditingController _emRel;
  late final TextEditingController _emPhone;
  late final TextEditingController _address;
  String _blood = '';
  bool _saving = false;
  bool _lastDirty = false;
  String? _error;

  DateTime get _now => (widget.clock ?? DateTime.now)();

  @override
  void initState() {
    super.initState();
    final d = ProfileDraft.from(widget.profile);
    TextEditingController c(String v) => TextEditingController(text: v)..addListener(_notify);
    _dob = c(d.dob);
    _anniv = c(d.anniversary);
    _personalEmail = c(d.personalEmail);
    _phone = c(d.phone);
    _emName = c(d.emergencyName);
    _emRel = c(d.emergencyRelationship);
    _emPhone = c(d.emergencyPhone);
    _address = c(d.address);
    _blood = d.bloodGroup;
  }

  @override
  void dispose() {
    for (final c in [_dob, _anniv, _personalEmail, _phone, _emName, _emRel, _emPhone, _address]) {
      c.dispose();
    }
    super.dispose();
  }

  ProfileDraft get _draft => ProfileDraft(
        dob: _dob.text,
        anniversary: _anniv.text,
        personalEmail: _personalEmail.text,
        phone: _phone.text,
        emergencyName: _emName.text,
        emergencyRelationship: _emRel.text,
        emergencyPhone: _emPhone.text,
        bloodGroup: _blood,
        address: _address.text,
      );

  void _notify() {
    final dirty = !_draft.sameAs(widget.profile);
    if (dirty != _lastDirty) {
      _lastDirty = dirty;
      widget.onDirtyChanged?.call(dirty);
    }
  }

  Future<void> _pickDate(TextEditingController c, {required bool dob}) async {
    final now = _now;
    final today = DateTime(now.year, now.month, now.day);
    final initial = parseYmd(c.text) ?? (dob ? DateTime(now.year - 30) : DateTime(now.year - 1));
    final first = dob ? DateTime(now.year - 100, now.month, now.day) : DateTime(1950);
    final last = dob ? DateTime(now.year - 16, now.month, now.day) : today.subtract(const Duration(days: 1));
    final picked = await showDatePicker(
      context: context,
      initialDate: initial.isBefore(first) ? first : (initial.isAfter(last) ? last : initial),
      firstDate: first,
      lastDate: last,
    );
    if (picked != null) c.text = ymd(picked);
  }

  Future<void> _save() async {
    if (!_formKey.currentState!.validate()) return;
    final body = _draft.diff(widget.profile);
    if (body.isEmpty) {
      widget.onCancel();
      return;
    }
    setState(() {
      _saving = true;
      _error = null;
    });
    final err = await widget.onSave(body);
    if (!mounted) return;
    setState(() {
      _saving = false;
      _error = err;
    });
  }

  Widget _dateField(
    TextEditingController c,
    String label, {
    required bool dob,
    required FormFieldValidator<String> validator,
  }) =>
      TextFormField(
        controller: c,
        readOnly: true,
        enabled: !_saving,
        decoration: InputDecoration(
          labelText: label,
          hintText: 'YYYY-MM-DD',
          suffixIcon: c.text.isEmpty
              ? const Icon(Icons.calendar_today_outlined)
              : IconButton(
                  tooltip: 'Clear $label',
                  icon: const Icon(Icons.clear),
                  onPressed: () {
                    c.clear();
                    setState(() {});
                  },
                ),
        ),
        onTap: () async {
          await _pickDate(c, dob: dob);
          if (mounted) setState(() {});
        },
        validator: validator,
      );

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    const gap = SizedBox(height: 12);
    return Form(
      key: _formKey,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text('Personal', style: theme.textTheme.titleMedium),
          gap,
          _dateField(_dob, 'Date of birth', dob: true, validator: (v) => validateDob(v, _now)),
          gap,
          _dateField(_anniv, 'Wedding anniversary', dob: false, validator: (v) => validateAnniversary(v, _now)),
          const SizedBox(height: 4),
          Text(
            'Only you and HR see these dates unless you switch on "Show my '
            'birthday/anniversary to colleagues" on your profile. $celebrationsExplanation',
            style: theme.textTheme.bodySmall,
          ),
          gap,
          DropdownButtonFormField<String>(
            initialValue: _blood.isEmpty ? null : _blood,
            decoration: const InputDecoration(labelText: 'Blood group'),
            items: [
              for (final g in bloodGroups) DropdownMenuItem(value: g, child: Text(g)),
            ],
            onChanged: _saving
                ? null
                : (v) {
                    setState(() => _blood = v ?? '');
                    _notify();
                  },
          ),
          const SizedBox(height: 20),
          Text('Contact', style: theme.textTheme.titleMedium),
          gap,
          TextFormField(
            controller: _personalEmail,
            enabled: !_saving,
            keyboardType: TextInputType.emailAddress,
            autofillHints: const [AutofillHints.email],
            decoration: const InputDecoration(labelText: 'Personal email'),
            validator: validateEmail,
          ),
          gap,
          TextFormField(
            controller: _phone,
            enabled: !_saving,
            keyboardType: TextInputType.phone,
            autofillHints: const [AutofillHints.telephoneNumber],
            decoration: const InputDecoration(labelText: 'Phone', hintText: '+919999999999'),
            validator: validatePhone,
          ),
          const SizedBox(height: 20),
          Text('Emergency contact', style: theme.textTheme.titleMedium),
          gap,
          TextFormField(
            controller: _emName,
            enabled: !_saving,
            textCapitalization: TextCapitalization.words,
            decoration: const InputDecoration(labelText: 'Name'),
            validator: (v) => validateEmergencyName(v, _emPhone.text),
          ),
          gap,
          TextFormField(
            controller: _emRel,
            enabled: !_saving,
            textCapitalization: TextCapitalization.words,
            decoration: const InputDecoration(labelText: 'Relationship (optional)'),
          ),
          gap,
          TextFormField(
            controller: _emPhone,
            enabled: !_saving,
            keyboardType: TextInputType.phone,
            decoration: const InputDecoration(labelText: 'Phone'),
            validator: (v) => validateEmergencyPhone(_emName.text, v),
          ),
          const SizedBox(height: 20),
          Text('Address', style: theme.textTheme.titleMedium),
          gap,
          TextFormField(
            controller: _address,
            enabled: !_saving,
            minLines: 2,
            maxLines: 4,
            maxLength: maxAddressLength,
            keyboardType: TextInputType.multiline,
            decoration: const InputDecoration(labelText: 'Home address', alignLabelWithHint: true),
            validator: validateAddress,
          ),
          if (_error != null)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8),
              child: Semantics(
                liveRegion: true,
                child: Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
              ),
            ),
          const SizedBox(height: 8),
          FilledButton(
            onPressed: _saving ? null : _save,
            child: _saving
                ? const SizedBox(width: 22, height: 22, child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('Save'),
          ),
          const SizedBox(height: 8),
          OutlinedButton(
            onPressed: _saving ? null : widget.onCancel,
            style: OutlinedButton.styleFrom(minimumSize: const Size.fromHeight(48)),
            child: const Text('Cancel'),
          ),
        ],
      ),
    );
  }
}
