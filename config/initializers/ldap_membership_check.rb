# frozen_string_literal: true

# LDAP/AD group-membership lookup, used by:
# - config/initializers/ldap_activation_hook.rb  (synchronous, single-uid check)
# - config/initializers/ldap_periodic_recheck.rb  (bulk re-check of LDAP_ACTIVATION_GROUP_DN)
# - config/initializers/ldap_group_sync.rb        (bulk check per configured AD group mapping)
#
# All LDAP settings are read from the environment (.env file); leave LDAP_HOST/LDAP_BASE blank
# to disable every feature built on top of this module.
module LdapMembershipCheck
  NESTED_GROUP_MATCHING_RULE = '1.2.840.113556.1.4.1941'
  USER_OBJECT_FILTER = '(objectClass=user)(objectCategory=person)'

  def self.enabled?
    ENV['LDAP_HOST'].present? && ENV['LDAP_BASE'].present?
  end

  def self.member?(group_dn, uid)
    return false if uid.blank? || group_dn.blank?

    search(filter: membership_filter(group_dn, uid), attributes: ['dn']).present?
  end

  # Single-query to return all members of a group as a Set of lowercase uid values as strings.
  def self.members(group_dn)
    attribute = uid_attribute
    entries = search(filter: membership_filter(group_dn), attributes: [attribute], paged_searches: true)
    entries.filter_map { |entry| entry[attribute]&.first&.downcase }.to_set
  end

  def self.membership_filter(group_dn, uid = nil)
    raise ArgumentError, 'group_dn cannot be blank' if group_dn.blank?

    group_dn_value = Net::LDAP::Filter.escape(group_dn)
    uid_clause = uid ? "(#{uid_attribute}=#{Net::LDAP::Filter.escape(uid)})" : ''
    Net::LDAP::Filter.construct(
      "(&#{uid_clause}#{USER_OBJECT_FILTER}(memberOf:#{NESTED_GROUP_MATCHING_RULE}:=#{group_dn_value}))",
    )
  end

  # Raises rather than returning nil, which is what Net::LDAP::Connection#search itself does on
  # a failed search (e.g. a bind failure).
  def self.search(filter:, attributes:, paged_searches: false)
    conn = connection
    result = conn.search(base: ENV.fetch('LDAP_BASE', nil), filter: filter, attributes: attributes,
                         paged_searches: paged_searches)
    return result unless result.nil?

    raise Net::LDAP::Error, conn.get_operation_result.message
  end

  def self.uid_attribute
    ENV['LDAP_UID_ATTRIBUTE'].presence || 'sAMAccountName'
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
