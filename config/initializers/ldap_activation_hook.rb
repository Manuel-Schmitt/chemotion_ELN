# frozen_string_literal: true

# Registers a synchronous LDAP/AD membership check as a before_create hook, from the outside,
# without touching app/models/user.rb.
#
# Intended to be dropped onto an already running production system (e.g. as a hotfix
# initializer): before a disabled Person account is persisted (see User#set_account_active /
# ENV['DEVISE_NEW_ACCOUNT_INACTIVE']), it is checked against a configured LDAP filter.
# Members are activated immediately (account_active is flipped before the INSERT, so no
# separate update is needed); non-members, and registrations where the directory itself is
# unreachable, are rejected and the record is never created at all.
#
# All LDAP settings are read from the environment; leave LDAP_HOST/LDAP_BASE/
# LDAP_ACTIVATION_FILTER blank to disable the whole feature.
module LdapMembershipCheck
  def self.enabled?
    ENV['LDAP_HOST'].present? && ENV['LDAP_BASE'].present? && ENV['LDAP_ACTIVATION_FILTER'].present?
  end

  # @return [Boolean] whether uid matches LDAP_ACTIVATION_FILTER (e.g. an AD nested-group
  #   membership filter using the 1.2.840.113556.1.4.1941 matching rule). Raises
  #   Net::LDAP::Error if the directory itself is unreachable.
  def self.member?(uid)
    return false if uid.blank?

    filter = activation_filter(uid)
    return false if filter.blank?

    connection.search(base: ENV.fetch('LDAP_BASE', nil), filter: filter, attributes: ['dn']).present?
  end

  # LDAP_ACTIVATION_FILTER is a full RFC4515 filter string with a %<uid>s placeholder for the
  # (escaped) directory account id, e.g.:
  #   (&(sAMAccountName=%<uid>s)(memberOf:1.2.840.113556.1.4.1941:=cn=eln-users,ou=groups,dc=example,dc=org))
  def self.activation_filter(uid)
    template = ENV.fetch('LDAP_ACTIVATION_FILTER', nil)
    return if template.blank?

    Net::LDAP::Filter.construct(format(template, uid: Net::LDAP::Filter.escape(uid)))
  end

  def self.connection
    Net::LDAP.new(
      host: ENV.fetch('LDAP_HOST', nil),
      port: ENV['LDAP_PORT'].presence || 389,
      base: ENV.fetch('LDAP_BASE', nil),
      encryption: encryption_options,
      auth: bind_auth,
    )
  end

  def self.bind_auth
    bind_dn = ENV.fetch('LDAP_BIND_DN', nil)
    return { method: :anonymous } if bind_dn.blank?

    { method: :simple, username: bind_dn, password: ENV.fetch('LDAP_BIND_PASSWORD', nil) }
  end

  def self.encryption_options
    case ENV['LDAP_ENCRYPTION_METHOD'].to_s
    when 'ssl' then { method: :simple_tls }
    when 'tls' then { method: :start_tls }
    end
  end
end

# `to_prepare` re-registers the callback every time the app is (re)loaded; guard against
# duplicate registration so repeated code reloads in development don't run the check twice.
Rails.application.config.to_prepare do
  next if User._create_callbacks.any? { |cb| cb.kind == :before && cb.filter == :ldap_activation_check }

  User.before_create(
    :ldap_activation_check,
    if: proc { |user| user.type == 'Person' && !user.account_active? && LdapMembershipCheck.enabled? },
  )

  User.send(:define_method, :ldap_activation_check) do
    account_attribute = ENV['LDAP_ACCOUNT_ATTRIBUTE'].presence || 'name_abbreviation'
    uid = public_send(account_attribute)

    if LdapMembershipCheck.member?(uid)
      self.account_active = true
    else
      errors.add(:base, 'is not a member of the required LDAP group')
      throw :abort
    end
  rescue Net::LDAP::Error => e
    # Block registration rather than silently creating a disabled account we can't verify.
    Rails.logger.error("LDAP group membership lookup failed for #{uid}: #{e.message}")
    errors.add(:base, 'could not be verified against the LDAP directory, please try again later')
    throw :abort
  end
  User.send(:private, :ldap_activation_check)
end
